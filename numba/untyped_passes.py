from __future__ import print_function, division, absolute_import
from collections import defaultdict, namedtuple
from copy import deepcopy, copy

from .compiler_machinery import FunctionPass, register_pass
from . import (config, bytecode, interpreter, postproc, errors, types, rewrites,
               transforms, ir, utils)
from .special import literal_unroll
import warnings
from .analysis import (
    dead_branch_prune,
    rewrite_semantic_constants,
    find_literally_calls,
    compute_cfg_from_blocks,
    compute_use_defs,
)
from contextlib import contextmanager
from .inline_closurecall import InlineClosureCallPass, inline_closure_call
from .ir_utils import (guard, resolve_func_from_module, simplify_CFG,
                       GuardException,  convert_code_obj_to_function,
                       mk_unique_var, build_definitions,
                       replace_var_names, get_name_var_table,
                       compile_to_numba_ir,)


@contextmanager
def fallback_context(state, msg):
    """
    Wraps code that would signal a fallback to object mode
    """
    try:
        yield
    except Exception as e:
        if not state.status.can_fallback:
            raise
        else:
            if utils.PYVERSION >= (3,):
                # Clear all references attached to the traceback
                e = e.with_traceback(None)
            # this emits a warning containing the error message body in the
            # case of fallback from npm to objmode
            loop_lift = '' if state.flags.enable_looplift else 'OUT'
            msg_rewrite = ("\nCompilation is falling back to object mode "
                           "WITH%s looplifting enabled because %s"
                           % (loop_lift, msg))
            warnings.warn_explicit('%s due to: %s' % (msg_rewrite, e),
                                   errors.NumbaWarning,
                                   state.func_id.filename,
                                   state.func_id.firstlineno)
            raise


@register_pass(mutates_CFG=True, analysis_only=False)
class ExtractByteCode(FunctionPass):
    _name = "extract_bytecode"

    def __init__(self):
        FunctionPass.__init__(self)

    def run_pass(self, state):
        """
        Extract bytecode from function
        """
        func_id = state['func_id']
        bc = bytecode.ByteCode(func_id)
        if config.DUMP_BYTECODE:
            print(bc.dump())

        state['bc'] = bc
        return True


@register_pass(mutates_CFG=True, analysis_only=False)
class TranslateByteCode(FunctionPass):
    _name = "translate_bytecode"

    def __init__(self):
        FunctionPass.__init__(self)

    def run_pass(self, state):
        """
        Analyze bytecode and translating to Numba IR
        """
        func_id = state['func_id']
        bc = state['bc']
        interp = interpreter.Interpreter(func_id)
        func_ir = interp.interpret(bc)
        state["func_ir"] = func_ir
        return True


@register_pass(mutates_CFG=True, analysis_only=False)
class FixupArgs(FunctionPass):
    _name = "fixup_args"

    def __init__(self):
        FunctionPass.__init__(self)

    def run_pass(self, state):
        state['nargs'] = state['func_ir'].arg_count
        if not state['args'] and state['flags'].force_pyobject:
            # Allow an empty argument types specification when object mode
            # is explicitly requested.
            state['args'] = (types.pyobject,) * state['nargs']
        elif len(state['args']) != state['nargs']:
            raise TypeError("Signature mismatch: %d argument types given, "
                            "but function takes %d arguments"
                            % (len(state['args']), state['nargs']))
        return True


@register_pass(mutates_CFG=True, analysis_only=False)
class IRProcessing(FunctionPass):
    _name = "ir_processing"

    def __init__(self):
        FunctionPass.__init__(self)

    def run_pass(self, state):
        func_ir = state['func_ir']
        post_proc = postproc.PostProcessor(func_ir)
        post_proc.run()

        if config.DEBUG or config.DUMP_IR:
            name = func_ir.func_id.func_qualname
            print(("IR DUMP: %s" % name).center(80, "-"))
            func_ir.dump()
            if func_ir.is_generator:
                print(("GENERATOR INFO: %s" % name).center(80, "-"))
                func_ir.dump_generator_info()
        return True


@register_pass(mutates_CFG=True, analysis_only=False)
class RewriteSemanticConstants(FunctionPass):
    _name = "rewrite_semantic_constants"

    def __init__(self):
        FunctionPass.__init__(self)

    def run_pass(self, state):
        """
        This prunes dead branches, a dead branch is one which is derivable as
        not taken at compile time purely based on const/literal evaluation.
        """
        assert state.func_ir
        msg = ('Internal error in pre-inference dead branch pruning '
               'pass encountered during compilation of '
               'function "%s"' % (state.func_id.func_name,))
        with fallback_context(state, msg):
            rewrite_semantic_constants(state.func_ir, state.args)

        return True


@register_pass(mutates_CFG=True, analysis_only=False)
class DeadBranchPrune(FunctionPass):
    _name = "dead_branch_prune"

    def __init__(self):
        FunctionPass.__init__(self)

    def run_pass(self, state):
        """
        This prunes dead branches, a dead branch is one which is derivable as
        not taken at compile time purely based on const/literal evaluation.
        """

        # purely for demonstration purposes, obtain the analysis from a pass
        # declare as a required dependent
        semantic_const_analysis = self.get_analysis(type(self))  # noqa

        assert state.func_ir
        msg = ('Internal error in pre-inference dead branch pruning '
               'pass encountered during compilation of '
               'function "%s"' % (state.func_id.func_name,))
        with fallback_context(state, msg):
            dead_branch_prune(state.func_ir, state.args)

        return True

    def get_analysis_usage(self, AU):
        AU.add_required(RewriteSemanticConstants)


@register_pass(mutates_CFG=True, analysis_only=False)
class InlineClosureLikes(FunctionPass):
    _name = "inline_closure_likes"

    def __init__(self):
        FunctionPass.__init__(self)

    def run_pass(self, state):
        # Ensure we have an IR and type information.
        assert state.func_ir

        # if the return type is a pyobject, there's no type info available and
        # no ability to resolve certain typed function calls in the array
        # inlining code, use this variable to indicate
        typed_pass = not isinstance(state.return_type, types.misc.PyObject)
        inline_pass = InlineClosureCallPass(
            state.func_ir,
            state.flags.auto_parallel,
            state.parfor_diagnostics.replaced_fns,
            typed_pass)
        inline_pass.run()
        # Remove all Dels, and re-run postproc
        post_proc = postproc.PostProcessor(state.func_ir)
        post_proc.run()

        if config.DEBUG or config.DUMP_IR:
            name = state.func_ir.func_id.func_qualname
            print(("IR DUMP: %s" % name).center(80, "-"))
            state.func_ir.dump()
        return True


@register_pass(mutates_CFG=True, analysis_only=False)
class GenericRewrites(FunctionPass):
    _name = "generic_rewrites"

    def __init__(self):
        FunctionPass.__init__(self)

    def run_pass(self, state):
        """
        Perform any intermediate representation rewrites before type
        inference.
        """
        assert state.func_ir
        msg = ('Internal error in pre-inference rewriting '
               'pass encountered during compilation of '
               'function "%s"' % (state.func_id.func_name,))
        with fallback_context(state, msg):
            rewrites.rewrite_registry.apply('before-inference', state)
        return True


@register_pass(mutates_CFG=True, analysis_only=False)
class WithLifting(FunctionPass):
    _name = "with_lifting"

    def __init__(self):
        FunctionPass.__init__(self)

    def run_pass(self, state):
        """
        Extract with-contexts
        """
        main, withs = transforms.with_lifting(
            func_ir=state.func_ir,
            typingctx=state.typingctx,
            targetctx=state.targetctx,
            flags=state.flags,
            locals=state.locals,
        )
        if withs:
            from numba.compiler import compile_ir, _EarlyPipelineCompletion
            cres = compile_ir(state.typingctx, state.targetctx, main,
                              state.args, state.return_type,
                              state.flags, state.locals,
                              lifted=tuple(withs), lifted_from=None,
                              pipeline_class=type(state.pipeline))
            raise _EarlyPipelineCompletion(cres)
        return True


@register_pass(mutates_CFG=True, analysis_only=False)
class InlineInlinables(FunctionPass):
    """
    This pass will inline a function wrapped by the numba.jit decorator directly
    into the site of its call depending on the value set in the 'inline' kwarg
    to the decorator.

    This is an untyped pass. CFG simplification is performed at the end of the
    pass but no block level clean up is performed on the mutated IR (typing
    information is not available to do so).
    """
    _name = "inline_inlinables"
    _DEBUG = False

    def __init__(self):
        FunctionPass.__init__(self)

    def run_pass(self, state):
        """Run inlining of inlinables
        """
        if self._DEBUG:
            print('before inline'.center(80, '-'))
            print(state.func_ir.dump())
            print(''.center(80, '-'))
        modified = False
        # use a work list, look for call sites via `ir.Expr.op == call` and
        # then pass these to `self._do_work` to make decisions about inlining.
        work_list = list(state.func_ir.blocks.items())
        while work_list:
            label, block = work_list.pop()
            for i, instr in enumerate(block.body):
                if isinstance(instr, ir.Assign):
                    expr = instr.value
                    if isinstance(expr, ir.Expr) and expr.op == 'call':
                        if guard(self._do_work, state, work_list, block, i,
                                 expr):
                            modified = True
                            break  # because block structure changed

        if modified:
            # clean up unconditional branches that appear due to inlined
            # functions introducing blocks
            state.func_ir.blocks = simplify_CFG(state.func_ir.blocks)

        if self._DEBUG:
            print('after inline'.center(80, '-'))
            print(state.func_ir.dump())
            print(''.center(80, '-'))
        return True

    def _do_work(self, state, work_list, block, i, expr):
        from numba.inline_closurecall import (inline_closure_call,
                                              callee_ir_validator)
        from numba.compiler import run_frontend
        from numba.targets.cpu import InlineOptions

        # try and get a definition for the call, this isn't always possible as
        # it might be a eval(str)/part generated awaiting update etc. (parfors)
        to_inline = None
        try:
            to_inline = state.func_ir.get_definition(expr.func)
        except Exception:
            if self._DEBUG:
                print("Cannot find definition for %s" % expr.func)
            return False
        # do not handle closure inlining here, another pass deals with that.
        if getattr(to_inline, 'op', False) == 'make_function':
            return False

        # see if the definition is a "getattr", in which case walk the IR to
        # try and find the python function via the module from which it's
        # imported, this should all be encoded in the IR.
        if getattr(to_inline, 'op', False) == 'getattr':
            val = resolve_func_from_module(state.func_ir, to_inline)
        else:
            # This is likely a freevar or global
            #
            # NOTE: getattr 'value' on a call may fail if it's an ir.Expr as
            # getattr is overloaded to look in _kws.
            try:
                val = getattr(to_inline, 'value', False)
            except Exception:
                raise GuardException

        # if something was found...
        if val:
            # check it's dispatcher-like, the targetoptions attr holds the
            # kwargs supplied in the jit decorator and is where 'inline' will
            # be if it is present.
            topt = getattr(val, 'targetoptions', False)
            if topt:
                inline_type = topt.get('inline', None)
                # has 'inline' been specified?
                if inline_type is not None:
                    inline_opt = InlineOptions(inline_type)
                    # Could this be inlinable?
                    if not inline_opt.is_never_inline:
                        # yes, it could be inlinable
                        do_inline = True
                        pyfunc = val.py_func
                        # Has it got an associated cost model?
                        if inline_opt.has_cost_model:
                            # yes, it has a cost model, use it to determine
                            # whether to do the inline
                            py_func_ir = run_frontend(pyfunc)
                            do_inline = inline_type(expr, state.func_ir,
                                                    py_func_ir)
                        # if do_inline is True then inline!
                        if do_inline:
                            inline_closure_call(
                                state.func_ir,
                                pyfunc.__globals__,
                                block, i, pyfunc,
                                work_list=work_list,
                                callee_validator=callee_ir_validator)
                            return True
        return False


@register_pass(mutates_CFG=False, analysis_only=False)
class PreserveIR(FunctionPass):
    """
    Preserves the IR in the metadata
    """

    _name = "preserve_ir"

    def __init__(self):
        FunctionPass.__init__(self)

    def run_pass(self, state):
        state.metadata['preserved_ir'] = state.func_ir.copy()
        return False


@register_pass(mutates_CFG=False, analysis_only=True)
class FindLiterallyCalls(FunctionPass):
    """Find calls to `numba.literally()` and signal if its requirement is not
    satisfied.
    """
    _name = "find_literally"

    def __init__(self):
        FunctionPass.__init__(self)

    def run_pass(self, state):
        find_literally_calls(state.func_ir, state.args)
        return False


@register_pass(mutates_CFG=True, analysis_only=False)
class MakeFunctionToJitFunction(FunctionPass):
    """
    This swaps an ir.Expr.op == "make_function" i.e. a closure, for a compiled
    function containing the closure body and puts it in ir.Global. It's a 1:1
    statement value swap. `make_function` is already untyped
    """
    _name = "make_function_op_code_to_jit_function"

    def __init__(self):
        FunctionPass.__init__(self)

    def run_pass(self, state):
        from numba import njit
        func_ir = state.func_ir
        mutated = False
        for idx, blk in func_ir.blocks.items():
            for stmt in blk.body:
                if isinstance(stmt, ir.Assign):
                    if isinstance(stmt.value, ir.Expr):
                        if stmt.value.op == "make_function":
                            node = stmt.value
                            getdef = func_ir.get_definition
                            kw_default = getdef(node.defaults)
                            ok = False
                            if (kw_default is None or
                                    isinstance(kw_default, ir.Const)):
                                ok = True
                            elif isinstance(kw_default, tuple):
                                ok = all([isinstance(getdef(x), ir.Const)
                                          for x in kw_default])

                            if not ok:
                                continue

                            pyfunc = convert_code_obj_to_function(node, func_ir)
                            func = njit()(pyfunc)
                            new_node = ir.Global(node.code.co_name, func,
                                                 stmt.loc)
                            stmt.value = new_node
                            mutated |= True

        # if a change was made the del ordering is probably wrong, patch up
        if mutated:
            post_proc = postproc.PostProcessor(func_ir)
            post_proc.run()

        return mutated


@register_pass(mutates_CFG=True, analysis_only=False)
class MixedContainerUnroller(FunctionPass):
    _name = "mixed_container_unroller"

    _DEBUG = False

    _accepted_types = (types.Tuple, types.UniTuple)

    def __init__(self):
        FunctionPass.__init__(self)

    def analyse_tuple(self, tup):
        """
        Returns a map of type->list(indexes) for a typed tuple
        """
        d = defaultdict(list)
        for i, ty in enumerate(tup):
            d[ty].append(i)
        return d

    def add_offset_to_labels_w_ignore(self, blocks, offset, ignore=None):
        """add an offset to all block labels and jump/branch targets
        don't add an offset to anything in the ignore list
        """
        if ignore is None:
            ignore = set()

        new_blocks = {}
        for l, b in blocks.items():
            # some parfor last blocks might be empty
            term = None
            if b.body:
                term = b.body[-1]
            if isinstance(term, ir.Jump):
                if term.target not in ignore:
                    b.body[-1] = ir.Jump(term.target + offset, term.loc)
            if isinstance(term, ir.Branch):
                if term.truebr not in ignore:
                    new_true = term.truebr + offset
                else:
                    new_true = term.truebr

                if term.falsebr not in ignore:
                    new_false = term.falsebr + offset
                else:
                    new_false = term.falsebr
                b.body[-1] = ir.Branch(term.cond, new_true, new_false, term.loc)
            new_blocks[l + offset] = b
        return new_blocks

    def inject_loop_body(self, switch_ir, loop_ir, caller_max_label,
                         dont_replace, switch_data):
        """
        Injects the "loop body" held in `loop_ir` into `switch_ir` where ever
        there is a statement of the form `SENTINEL.<int> = RHS`. It also:
        * Finds and then deliberately does not relabel non-local jumps so as to
          make the switch table suitable for injection into the IR from which
          the loop body was derived.
        * Looks for `typed_getitem` and wires them up to loop body version
          specific variables or, if possible, directly writes in their constant
          value at their use site.

        Args:
        - switch_ir, the switch table with SENTINELS as generated by
          self.gen_switch
        - loop_ir, the IR of the loop blocks (derived from the original func_ir)
        - caller_max_label, the maximum label in the func_ir caller
        - dont_replace, variables that should not be renamed (to handle
          references to variables that are incoming at the loop head/escaping at
          the loop exit.
        - switch_data, the switch table data used to generated the switch_ir,
          can be generated by self.analyse_tuple.

        Returns:
        - A type specific switch table with each case containing a versioned
          loop body suitable for injection as a replacement for the loop_ir.
        """
        # Find the sentinels and validate the form
        sentinel_exits = set()
        sentinel_blocks = []
        for lbl, blk in switch_ir.blocks.items():
            for i, stmt in enumerate(blk.body):
                if isinstance(stmt, ir.Assign):
                    if "SENTINEL" in stmt.target.name:
                        sentinel_blocks.append(lbl)
                        sentinel_exits.add(blk.body[-1].target)
                        break

        assert len(sentinel_exits) == 1  # should only be 1 exit
        switch_ir.blocks.pop(sentinel_exits.pop())  # kill the exit, it's dead

        # find jumps that are non-local, we won't relabel these
        ignore_set = set()
        local_lbl = [x for x in loop_ir.blocks.keys()]
        for lbl, blk in loop_ir.blocks.items():
            for i, stmt in enumerate(blk.body):
                if isinstance(stmt, ir.Jump):
                    if stmt.target not in local_lbl:
                        ignore_set.add(stmt.target)
                if isinstance(stmt, ir.Branch):
                    if stmt.truebr not in local_lbl:
                        ignore_set.add(stmt.truebr)
                    if stmt.falsebr not in local_lbl:
                        ignore_set.add(stmt.falsebr)

        # make sure the generated switch table matches the switch data
        assert len(sentinel_blocks) == len(switch_data)

        # replace the sentinel_blocks with the loop body
        for lbl, branch_ty in zip(sentinel_blocks, switch_data.keys()):
            loop_blocks = deepcopy(loop_ir.blocks)
            # relabel blocks
            max_label = max(switch_ir.blocks.keys())
            loop_blocks = self.add_offset_to_labels_w_ignore(
                loop_blocks, max_label + 1, ignore_set)

            # start label
            loop_start_lbl = min(loop_blocks.keys())

            # fix the typed_getitem locations in the loop blocks
            for blk in loop_blocks.values():
                new_body = []
                for stmt in blk.body:
                    if isinstance(stmt, ir.Assign):
                        if (isinstance(stmt.value, ir.Expr) and
                                stmt.value.op == "typed_getitem"):
                            if isinstance(branch_ty, types.Literal):
                                new_const_name = mk_unique_var("branch_const")
                                new_const_var = ir.Var(
                                    blk.scope, new_const_name, stmt.loc)
                                new_const_val = ir.Const(
                                    branch_ty.literal_value, stmt.loc)
                                const_assign = ir.Assign(
                                    new_const_val, new_const_var, stmt.loc)
                                new_assign = ir.Assign(
                                    new_const_var, stmt.target, stmt.loc)
                                new_body.append(const_assign)
                                new_body.append(new_assign)
                                dont_replace.append(new_const_name)
                            else:
                                orig = stmt.value
                                new_typed_getitem = ir.Expr.typed_getitem(
                                    value=orig.value, dtype=branch_ty,
                                    index=orig.index, loc=orig.loc)
                                new_assign = ir.Assign(
                                    new_typed_getitem, stmt.target, stmt.loc)
                                new_body.append(new_assign)
                        else:
                            new_body.append(stmt)
                    else:
                        new_body.append(stmt)
                blk.body = new_body

            # rename
            var_table = get_name_var_table(loop_blocks)
            drop_keys = []
            for k, v in var_table.items():
                if v.name in dont_replace:
                    drop_keys.append(k)
            for k in drop_keys:
                var_table.pop(k)

            new_var_dict = {}
            for name, var in var_table.items():
                new_var_dict[name] = mk_unique_var(name)
            replace_var_names(loop_blocks, new_var_dict)

            # clobber the sentinel body and then stuff in the rest
            switch_ir.blocks[lbl] = deepcopy(loop_blocks[loop_start_lbl])
            remaining_keys = [y for y in loop_blocks.keys()]
            remaining_keys.remove(loop_start_lbl)
            for k in remaining_keys:
                switch_ir.blocks[k] = deepcopy(loop_blocks[k])

        # now relabel the switch_ir WRT the caller max label
        switch_ir.blocks = self.add_offset_to_labels_w_ignore(
            switch_ir.blocks, caller_max_label + 1, ignore_set)

        if self._DEBUG:
            print("-" * 80 + "EXIT STUFFER")
            switch_ir.dump()
            print("-" * 80)

        return switch_ir

    def gen_switch(self, data, index):
        """
        Generates a function with a switch table like
        def foo():
            if PLACEHOLDER_INDEX in (<integers>):
                SENTINEL = None
            elif PLACEHOLDER_INDEX in (<integers>):
                SENTINEL = None
            ...
            else:
                raise RuntimeError

        The data is a map of (type : indexes) for example:
        (int64, int64, float64)
        might give:
        {int64: [0, 1], float64: [2]}

        The index is the index variable for the driving range loop over the
        mixed tuple.
        """
        elif_tplt = "\n\telif PLACEHOLDER_INDEX in (%s,):\n\t\tSENTINEL = None"

        b = ('def foo():\n\tif PLACEHOLDER_INDEX in (%s,):\n\t\t'
             'SENTINEL = None\n%s\n\telse:\n\t\t'
             'raise RuntimeError("Unreachable")')
        keys = [k for k in data.keys()]

        elifs = []
        for i in range(1, len(keys)):
            elifs.append(elif_tplt % ','.join(map(str, data[keys[i]])))
        src = b % (','.join(map(str, data[keys[0]])), ''.join(elifs))
        wstr = src
        l = {}
        exec(wstr, {}, l)
        bfunc = l['foo']
        branches = compile_to_numba_ir(bfunc, {})
        for lbl, blk in branches.blocks.items():
            for stmt in blk.body:
                if isinstance(stmt, ir.Assign):
                    if isinstance(stmt.value, ir.Global):
                        if stmt.value.name == "PLACEHOLDER_INDEX":
                            stmt.value = index
        return branches

    def apply_transform(self, state):
        # compute new CFG
        func_ir = state.func_ir
        cfg = compute_cfg_from_blocks(func_ir.blocks)
        # find loops
        loops = cfg.loops()

        # 0. Find the loops containing literal_unroll and store this
        #    information
        literal_unroll_info = dict()
        unroll_info = namedtuple(
            "unroll_info", [
                "loop", "call", "arg", "getitem"])

        for lbl, loop in loops.items():
            # TODO: check the loop head has literal_unroll, if it does but
            # does not conform to the following then raise

            # scan loop header
            iternexts = [
                *func_ir.blocks[loop.header].find_exprs('iternext')]
            if len(iternexts) != 1:
                return False
            for iternext in iternexts:
                # Walk the canonicalised loop structure and check it
                # Check loop form range(literal_unroll(container)))
                try:
                    phi = func_ir.get_definition(iternext.value)
                except Exception:
                    continue

                # check call global "range"
                range_call = func_ir.get_definition(phi.value)
                if not isinstance(range_call, ir.Expr):
                    continue
                if not range_call.op == "call":
                    continue
                range_global = func_ir.get_definition(range_call.func)
                if not isinstance(range_global, ir.Global):
                    continue
                if range_global.value is not range:
                    continue
                range_arg = range_call.args[0]

                # check call global "len"
                len_call = func_ir.get_definition(range_arg)
                if not isinstance(len_call, ir.Expr):
                    continue
                if not len_call.op == "call":
                    continue
                len_global = func_ir.get_definition(len_call.func)
                if not isinstance(len_global, ir.Global):
                    continue
                if len_global.value is not len:
                    continue
                len_arg = len_call.args[0]

                # check literal_unroll
                literal_unroll_call = func_ir.get_definition(len_arg)
                literal_func = getattr(literal_unroll_call, 'func', None)
                if not literal_func:
                    continue
                call_func = func_ir.get_definition(
                    literal_unroll_call.func).value
                if call_func is literal_unroll:
                    assert len(literal_unroll_call.args) == 1
                    arg = literal_unroll_call.args[0]
                    typemap = state.typemap
                    ty = typemap[arg.name]
                    assert isinstance(ty, self._accepted_types)
                    # loop header is spelled ok, now make sure the body
                    # actually contains a getitem

                    # find a "getitem"
                    tuple_getitem = None
                    for lbl in loop.body:
                        blk = func_ir.blocks[lbl]
                        for stmt in blk.body:
                            if isinstance(stmt, ir.Assign):
                                if isinstance(
                                        stmt.value, ir.Expr) and stmt.value.op == "getitem":
                                    # check for something like a[i]
                                    if stmt.value.value != arg:
                                        # that failed, so check for the
                                        # definition
                                        dfn = func_ir.get_definition(
                                            stmt.value.value)
                                        args = getattr(dfn, 'args', False)
                                        if not args:
                                            continue
                                        if not args[0] == arg:
                                            continue
                                    target_ty = state.typemap[arg.name]
                                    if not isinstance(target_ty,
                                                      self._accepted_types):
                                        continue
                                    tuple_getitem = stmt
                                    break
                        if tuple_getitem:
                            break
                    else:
                        continue  # no getitem in this loop

                    ui = unroll_info(loop, literal_unroll_call, arg,
                                        tuple_getitem)
                    literal_unroll_info[lbl] = ui

        if not literal_unroll_info:
            return False

        # Validate loops
        # 1. must not have any calls to literal_unroll
        for test_lbl, test_loop in literal_unroll_info.items():
            for ref_lbl, ref_loop in literal_unroll_info.items():
                if test_lbl == ref_lbl:  # comparing to self! skip
                    continue
                if test_loop.loop.header in ref_loop.loop.body:
                    msg = ("Nesting of literal_unroll is unsupported")
                    loc = func_ir.blocks[test_loop.loop.header].loc
                    raise errors.UnsupportedError(msg, loc)

        # 2. Do the unroll, get a loop and process it!
        lbl, info = literal_unroll_info.popitem()
        self.unroll_loop(state, info)
        func_ir.blocks = simplify_CFG(func_ir.blocks)
        post_proc = postproc.PostProcessor(func_ir)
        post_proc.run()
        if self._DEBUG:
            print('-' * 80 + "END OF PASS, SIMPLIFY DONE")
            func_ir.dump()
        # rebuild the definitions table, the IR has taken a hammering
        func_ir._definitions = build_definitions(func_ir.blocks)
        return True

    def unroll_loop(self, state, loop_info):
        # The general idea here is to:
        # 1. Find *a* getitem that conforms to the literal_unroll semantic,
        #    i.e. one that is targeting a tuple with a loop induced index
        # 2. Compute a structure from the tuple that describes which
        #    iterations of a loop will have which type
        # 3. Generate a switch table in IR form for the structure in 2
        # 4. Switch out getitems for the tuple for a `typed_getitem`
        # 5. Inject switch table as replacement loop body
        # 6. Patch up
        func_ir = state.func_ir
        getitem_target = loop_info.arg
        target_ty = state.typemap[getitem_target.name]
        assert isinstance(target_ty, self._accepted_types)

        # 1. find a "getitem" that conforms
        tuple_getitem = []
        for lbl in loop_info.loop.body:
            blk = func_ir.blocks[lbl]
            for stmt in blk.body:
                if isinstance(stmt, ir.Assign):
                    if isinstance(stmt.value,
                                    ir.Expr) and stmt.value.op == "getitem":
                        # try a couple of spellings... a[i] and ref(a)[i]
                        if stmt.value.value != getitem_target:
                            dfn = func_ir.get_definition(
                                stmt.value.value)
                            args = getattr(dfn, 'args', False)
                            if not args:
                                continue
                            if not args[0] == getitem_target:
                                continue
                        target_ty = state.typemap[getitem_target.name]
                        if not isinstance(target_ty, self._accepted_types):
                            continue
                        tuple_getitem.append(stmt)

        if not tuple_getitem:
            msg = ("Loop unrolling analysis has failed, there's no getitem "
                    "in loop body that conforms to literal_unroll "
                    "requirements.")
            LOC = func_ir.blocks[loop_info.loop.header].loc
            raise errors.CompilerError(msg, LOC)

        # 2. get switch data
        switch_data = self.analyse_tuple(target_ty)

        # 3. generate switch IR
        index = func_ir._definitions[tuple_getitem[0].value.index.name][0]
        branches = self.gen_switch(switch_data, index)

        # 4. swap getitems for a typed_getitem, these are actually just
        # placeholders at this point. When the loop is duplicated they can
        # be swapped for a typed_getitem of the correct type or if the item
        # is literal it can be shoved straight into the duplicated loop body
        for item in tuple_getitem:
            old = item.value
            new = ir.Expr.typed_getitem(
                old.value, types.void, old.index, old.loc)
            item.value = new

        # 5. Inject switch table

        # Find the actual loop without the header (that won't get replaced)
        # and derive some new IR for this set of blocks
        this_loop = loop_info.loop
        this_loop_body = this_loop.body - \
            set([this_loop.header])
        loop_blocks = {
            x: func_ir.blocks[x] for x in this_loop_body}
        new_ir = func_ir.derive(loop_blocks)

        # Work out what is live on entry and exit so as to prevent
        # replacement (defined vars can escape, used vars live at the header
        # need to remain as-is so their references are correct, they can
        # also escape).

        usedefs = compute_use_defs(func_ir.blocks)
        idx = this_loop.header
        keep = set()
        keep |= usedefs.usemap[idx] | usedefs.defmap[idx]
        keep |= func_ir.variable_lifetime.livemap[idx]
        dont_replace = [x for x in (keep)]

        # compute the unrolled body
        unrolled_body = self.inject_loop_body(
            branches, new_ir, max(func_ir.blocks.keys()),
            dont_replace, switch_data)

        # 6. Patch in the unrolled body and fix up
        blks = state.func_ir.blocks
        orig_lbl = tuple(this_loop_body)
        data = unrolled_body, this_loop.header
        replace, *delete = orig_lbl
        unroll, header_block = data
        unroll_lbl = [x for x in sorted(unroll.blocks.keys())]
        blks[replace] = unroll.blocks[unroll_lbl[0]]
        [blks.pop(d) for d in delete]
        for k in unroll_lbl[1:]:
            blks[k] = unroll.blocks[k]
        # stitch up the loop predicate true -> new loop body jump
        blks[header_block].body[-1].truebr = replace


    def run_pass(self, state):
        mutated = False
        func_ir = state.func_ir
        # first limit the work by squashing the CFG if possible
        func_ir.blocks = simplify_CFG(func_ir.blocks)

        if self._DEBUG:
            print("-" * 80 + "PASS ENTRY")
            func_ir.dump()
            print("-" * 80)

        # limitations:
        # 1. No nested unrolls
        # 2. Opt in via `numba.literal_unroll`
        # 3. No multiple mix-tuple use

        # TODO: Add the following in so that loop nests are walked in reverse
        # nesting order and as a result nested transforms become valid.
        def true_body(loop):
            # computes blocks reachable from a loop head
            return loop.body - {loop.header} - loop.exits

        def get_loop_nest(structure):
            all_children = []
            for x in structure.values():
                all_children.extend(x)

            # BFS order
            def BFS(root):
                acc = []

                def walk(node):
                    for x in structure[node]:
                        acc.append(x)
                    for x in structure[node]:
                        walk(x)
                walk(root)
                return acc

            nest = {}
            for x in structure.keys():
                if x not in all_children:
                    nest[x] = BFS(x)
            return nest

        def compute_loop_structure(loops):
            """
            Return BFS ordered list of loop nests
            """
            # a loop can only have one loop parent, but a loop parent may have
            # many children
            children = defaultdict(list)
            for loop in loops.values():
                for test_loop in loops.values():
                    if loop.header in true_body(test_loop):
                        # loop is a child of test_loop
                        children[test_loop.header].append(loop.header)
                else:
                    # loop is inner most
                    children[test_loop.header] = []
            # any loop which has children which also have children themselves
            # needs fixing up as children!=grandchildren
            ck = [x for x in children.keys()]
            for k in ck:
                for child in children[k][:]:
                    grandchildren = children[child]
                    for grandchild in grandchildren:
                        if grandchild in children[k]:
                            children[k].remove(grandchild)

            loop_nest = get_loop_nest(children)
            # any loops with that are not in children's keys are parents without
            # children
            for loop in loops.values():
                if loop.header not in children:
                    loop_nest[loop.header] = []

        # keep running the transform loop until it reports no more changes
        while(True):
            stat = self.apply_transform(state)
            mutated |= stat
            if not stat:
                break

        # reset type inference now we are done with the partial results
        state.typemap = {}
        state.return_type = None
        state.calltypes = None
        return mutated


@register_pass(mutates_CFG=True, analysis_only=False)
class IterLoopCanonicalization(FunctionPass):
    """ Transforms loops that are induced by `getiter` into range() driven loops
    If the typemap is available this will only impact Tuple and UniTuple, if it
    is not available it will impact all matching loops.
    """
    _name = "iter_loop_canonicalisation"

    _DEBUG = False

    # if partial typing info is available it will only look at these types
    _accepted_types = (types.Tuple, types.UniTuple)
    _accepted_calls = (literal_unroll,)

    def __init__(self):
        FunctionPass.__init__(self)

    def assess_loop(self, loop, func_ir, partial_typemap=None):
        # it's a iter loop if:
        # - loop header is driven by an iternext
        # - the iternext value is a phi derived from getiter()

        # check header
        iternexts = [*func_ir.blocks[loop.header].find_exprs('iternext')]
        if len(iternexts) != 1:
            return False
        for iternext in iternexts:
            try:
                phi = func_ir.get_definition(iternext.value)
            except Exception:
                return False
            if getattr(phi, 'op', False) == 'getiter':
                if partial_typemap:
                    # check that the call site is accepted, until we're
                    # confident that tuple unrolling is behaving require opt-in
                    # guard of `literal_unroll`, remove this later!
                    phi_val_defn = func_ir.get_definition(phi.value)
                    if not isinstance(phi_val_defn, ir.Expr):
                        return False
                    if not phi_val_defn.op == "call":
                        return False
                    call = func_ir.get_definition(phi_val_defn)
                    if len(call.args) != 1:
                        return False
                    func_var = func_ir.get_definition(call.func)
                    func = func_ir.get_definition(func_var)
                    if not isinstance(func, ir.Global):
                        return False
                    if func.value not in self._accepted_calls:
                        return False

                    # now check the type is supported
                    ty = partial_typemap.get(call.args[0].name, None)
                    if ty and isinstance(ty, self._accepted_types):
                        return len(loop.entries) == 1
                else:
                    return len(loop.entries) == 1

    def transform(self, loop, func_ir, cfg):
        def get_range(a):
            return range(len(a))

        iternext = [*func_ir.blocks[loop.header].find_exprs('iternext')][0]
        LOC = func_ir.blocks[loop.header].loc
        get_range_var = ir.Var(func_ir.blocks[loop.header].scope,
                               mk_unique_var('get_range_gbl'), LOC)
        get_range_global = ir.Global('get_range', get_range, LOC)
        assgn = ir.Assign(get_range_global, get_range_var, LOC)

        loop_entry = tuple(loop.entries)[0]
        entry_block = func_ir.blocks[loop_entry]
        entry_block.body.insert(0, assgn)

        iterarg = func_ir.get_definition(iternext.value).value

        # look for iternext
        idx = 0
        for stmt in entry_block.body:
            if isinstance(stmt, ir.Assign):
                if isinstance(stmt.value,
                              ir.Expr) and stmt.value.op == 'getiter':
                    break
            idx += 1
        else:
            raise ValueError("problem")

        # create a range(len(tup)) and inject it
        call_get_range_var = ir.Var(entry_block.scope,
                                    mk_unique_var('call_get_range'), LOC)
        make_call = ir.Expr.call(get_range_var, (stmt.value.value,), (), LOC)
        assgn_call = ir.Assign(make_call, call_get_range_var, LOC)
        entry_block.body.insert(idx, assgn_call)
        entry_block.body[idx + 1].value.value = call_get_range_var

        f = compile_to_numba_ir(get_range, {})
        glbls = copy(func_ir.func_id.func.__globals__)
        inline_closure_call(func_ir, glbls, entry_block, idx, get_range,)
        kill = entry_block.body.index(assgn)
        entry_block.body.pop(kill)

        # find the induction variable + references in the loop header
        # fixed point iter to do this, it's a bit clunky
        induction_vars = set()
        header_block = func_ir.blocks[loop.header]

        # find induction var
        ind = [x for x in header_block.find_exprs('pair_first')]
        for x in ind:
            induction_vars.add(func_ir.get_assignee(x, loop.header))
        # find aliases of the induction var
        tmp = set()
        for x in induction_vars:
            tmp.add(func_ir.get_assignee(x, loop.header))
        induction_vars |= tmp

        # Find the downstream blocks that might reference the induction var
        succ = set()
        for lbl in loop.exits:
            succ |= set([x[0] for x in cfg.successors(lbl)])
        check_blocks = (loop.body | loop.exits | succ) ^ {loop.header}

        # replace RHS use of induction var with getitem
        for lbl in check_blocks:
            for stmt in func_ir.blocks[lbl].body:
                if isinstance(stmt, ir.Assign):
                    if stmt.value in induction_vars:
                        stmt.value = ir.Expr.getitem(
                            iterarg, stmt.value, stmt.loc)

        post_proc = postproc.PostProcessor(func_ir)
        post_proc.run()

    def run_pass(self, state):
        func_ir = state.func_ir
        cfg = compute_cfg_from_blocks(func_ir.blocks)
        loops = cfg.loops()

        mutated = False
        accepted_loops = []
        for header, loop in loops.items():
            stat = self.assess_loop(loop, func_ir, state.typemap)
            if stat:
                if self._DEBUG:
                    print("Canonicalising loop", loop)
                self.transform(loop, func_ir, cfg)
                mutated = True
            else:
                if self._DEBUG:
                    print("NOT Canonicalising loop", loop)

        func_ir.blocks = simplify_CFG(func_ir.blocks)
        return mutated


@register_pass(mutates_CFG=True, analysis_only=False)
class SimplifyCFG(FunctionPass):
    """Perform CFG simplification"""
    _name = "simplify_cfg"

    def __init__(self):
        FunctionPass.__init__(self)

    def run_pass(self, state):
        blks = state.func_ir.blocks
        new_blks = simplify_CFG(blks)
        state.func_ir.blocks = new_blks
        mutated = blks != new_blks
        return mutated
