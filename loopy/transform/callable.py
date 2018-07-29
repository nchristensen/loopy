from __future__ import division, absolute_import

__copyright__ = "Copyright (C) 2018 Kaushik Kulkarni"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import six

import islpy as isl
from pymbolic.primitives import CallWithKwargs

from loopy.kernel import LoopKernel
from loopy.kernel.function_interface import CallableKernel
from pytools import ImmutableRecord
from loopy.diagnostic import LoopyError
from loopy.kernel.instruction import (CallInstruction, MultiAssignmentBase,
        CInstruction, _DataObliviousInstruction)
from loopy.symbolic import IdentityMapper, SubstitutionMapper, CombineMapper
from loopy.isl_helpers import simplify_via_aff
from loopy.kernel.function_interface import (get_kw_pos_association,
        change_names_of_pymbolic_calls)


__doc__ = """
.. currentmodule:: loopy

.. autofunction:: register_function_lookup

.. autofunction:: register_callable_kernel
"""


# {{{ register function lookup

def register_function_lookup(kernel, function_lookup):
    """
    Returns a copy of *kernel* with the *function_lookup* registered.

    :arg function_lookup: A function of signature ``(target, identifier)``
        returning a :class:`loopy.kernel.function_interface.InKernelCallable`.
    """

    # adding the function lookup to the set of function lookers in the kernel.
    if function_lookup not in kernel.function_scopers:
        from loopy.tools import unpickles_equally
        if not unpickles_equally(function_lookup):
            raise LoopyError("function '%s' does not "
                    "compare equally after being upickled "
                    "and would disrupt loopy's caches"
                    % function_lookup)
        new_function_scopers = kernel.function_scopers + [function_lookup]
    registered_kernel = kernel.copy(function_scopers=new_function_scopers)
    from loopy.kernel.creation import scope_functions

    # returning the scoped_version of the kernel, as new functions maybe
    # resolved.
    return scope_functions(registered_kernel)

# }}}


# {{{ register_callable_kernel

class _RegisterCalleeKernel(ImmutableRecord):
    """
    Helper class to make the function scoper from
    :func:`loopy.transform.register_callable_kernel` picklable. As python
    cannot pickle lexical closures.
    """
    fields = set(['function_name', 'callable_kernel'])

    def __init__(self, function_name, callable_kernel):
        self.function_name = function_name
        self.callable_kernel = callable_kernel

    def __call__(self, target, identifier):
        if identifier == self.function_name:
            return self.callable_kernel
        return None


def register_callable_kernel(caller_kernel, function_name, callee_kernel):
    """Returns a copy of *caller_kernel*, which would resolve *function_name* in an
    expression as a call to *callee_kernel*.

    :arg caller_kernel: An instance of :class:`loopy.kernel.LoopKernel`.
    :arg function_name: An instance of :class:`str`.
    :arg callee_kernel: An instance of :class:`loopy.kernel.LoopKernel`.
    """

    # {{{ sanity checks

    assert isinstance(caller_kernel, LoopKernel)
    assert isinstance(callee_kernel, LoopKernel)
    assert isinstance(function_name, str)

    # check to make sure that the variables with 'out' direction is equal to
    # the number of assigness in the callee kernel intructions.
    from loopy.kernel.tools import infer_arg_is_output_only
    callee_kernel = infer_arg_is_output_only(callee_kernel)
    expected_num_assignees = len([arg for arg in callee_kernel.args if
        arg.is_output_only])
    expected_num_parameters = len(callee_kernel.args) - expected_num_assignees
    for insn in caller_kernel.instructions:
        if isinstance(insn, CallInstruction) and (
                insn.expression.function.name == 'function_name'):
            if insn.assignees != expected_num_assignees:
                raise LoopyError("The number of arguments with 'out' direction "
                        "in callee kernel %s and the number of assignees in "
                        "instruction %s do not match." % (
                            callee_kernel.name, insn.id))
            if insn.expression.prameters != expected_num_parameters:
                raise LoopyError("The number of expected arguments "
                        "for the callee kernel %s and the number of parameters in "
                        "instruction %s do not match." % (
                            callee_kernel.name, insn.id))

        elif isinstance(insn, (MultiAssignmentBase, CInstruction,
                _DataObliviousInstruction)):
            pass
        else:
            raise NotImplementedError("unknown instruction %s" % type(insn))

    # }}}

    # making the target of the child kernel to be same as the target of parent
    # kernel.
    callable_kernel = CallableKernel(subkernel=callee_kernel.copy(
                        target=caller_kernel.target,
                        name=function_name,
                        is_called_from_host=False))

    # FIXME disabling global barriers for callee kernel (for now)
    from loopy import set_options
    callee_kernel = set_options(callee_kernel, "disable_global_barriers")

    return register_function_lookup(caller_kernel,
            _RegisterCalleeKernel(function_name, callable_kernel))

# }}}


# {{{ callee scoped calls collector (to support inlining)

class CalleeScopedCallsCollector(CombineMapper):
    """
    Collects the scoped functions which are a part of the callee kernel and
    must be transferred to the caller kernel before inlining.

    :returns:
        An :class:`frozenset` of function names that are not scoped in
        the caller kernel.

    .. note::
        :class:`loopy.library.reduction.ArgExtOp` are ignored, as they are
        never scoped in the pipeline.
    """

    def __init__(self, callee_scoped_functions):
        self.callee_scoped_functions = callee_scoped_functions

    def combine(self, values):
        import operator
        from functools import reduce
        return reduce(operator.or_, values, frozenset())

    def map_call(self, expr):
        if expr.function.name in self.callee_scoped_functions:
            return (frozenset([(expr,
                self.callee_scoped_functions[expr.function.name])]) |
                    self.combine((self.rec(child) for child in expr.parameters)))
        else:
            return self.combine((self.rec(child) for child in expr.parameters))

    def map_call_with_kwargs(self, expr):
        if expr.function.name in self.callee_scoped_functions:
            return (frozenset([(expr,
                self.callee_scoped_functions[expr.function.name])]) |
                    self.combine((self.rec(child) for child in expr.parameters
                        + tuple(expr.kw_parameters.values()))))
        else:
            return self.combine((self.rec(child) for child in
                expr.parameters+tuple(expr.kw_parameters.values())))

    def map_constant(self, expr):
        return frozenset()

    map_variable = map_constant
    map_function_symbol = map_constant
    map_tagged_variable = map_constant
    map_type_cast = map_constant

# }}}


# {{{ kernel inliner mapper

class KernelInliner(SubstitutionMapper):
    """Mapper to replace variables (indices, temporaries, arguments) in the
    callee kernel with variables in the caller kernel.

    :arg caller: the caller kernel
    :arg arg_map: dict of argument name to variables in caller
    :arg arg_dict: dict of argument name to arguments in callee
    """

    def __init__(self, subst_func, caller, arg_map, arg_dict):
        super(KernelInliner, self).__init__(subst_func)
        self.caller = caller
        self.arg_map = arg_map
        self.arg_dict = arg_dict

    def map_subscript(self, expr):
        if expr.aggregate.name in self.arg_map:

            aggregate = self.subst_func(expr.aggregate)
            sar = self.arg_map[expr.aggregate.name]  # SubArrayRef in caller
            callee_arg = self.arg_dict[expr.aggregate.name]  # Arg in callee
            if aggregate.name in self.caller.arg_dict:
                caller_arg = self.caller.arg_dict[aggregate.name]  # Arg in caller
            else:
                caller_arg = self.caller.temporary_variables[aggregate.name]

            # Firstly, map inner inames to outer inames.
            outer_indices = self.map_tuple(expr.index_tuple)

            # Next, reshape to match dimension of outer arrays.
            # We can have e.g. A[3, 2] from outside and B[6] from inside
            from numbers import Integral
            if not all(isinstance(d, Integral) for d in callee_arg.shape):
                raise LoopyError(
                    "Argument: {0} in callee kernel: {1} does not have "
                    "constant shape.".format(callee_arg))

            flatten_index = 0
            for i, idx in enumerate(sar.get_begin_subscript().index_tuple):
                flatten_index += idx*caller_arg.dim_tags[i].stride

            flatten_index += sum(
                idx * tag.stride
                for idx, tag in zip(outer_indices, callee_arg.dim_tags))

            from loopy.isl_helpers import simplify_via_aff
            flatten_index = simplify_via_aff(flatten_index)

            new_indices = []
            for dim_tag in caller_arg.dim_tags:
                ind = flatten_index // dim_tag.stride
                flatten_index -= (dim_tag.stride * ind)
                new_indices.append(ind)

            new_indices = tuple(simplify_via_aff(i) for i in new_indices)

            return aggregate.index(tuple(new_indices))
        else:
            return super(KernelInliner, self).map_subscript(expr)

# }}}


# {{{ inlining of a single call instruction

def _inline_call_instruction(kernel, callee_knl, instruction):
    """
    Returns a copy of *kernel* with the *instruction* in the *kernel*
    replaced by inlining :attr:`subkernel` within it.
    """
    callee_label = callee_knl.name[:4] + "_"

    # {{{ duplicate and rename inames

    vng = kernel.get_var_name_generator()
    ing = kernel.get_instruction_id_generator()
    dim_type = isl.dim_type.set

    iname_map = {}
    for iname in callee_knl.all_inames():
        iname_map[iname] = vng(callee_label+iname)

    new_domains = []
    new_iname_to_tags = kernel.iname_to_tags.copy()

    # transferring iname tags info from the callee to the caller kernel
    for domain in callee_knl.domains:
        new_domain = domain.copy()
        for i in range(new_domain.n_dim()):
            iname = new_domain.get_dim_name(dim_type, i)

            if iname in callee_knl.iname_to_tags:
                new_iname_to_tags[iname_map[iname]] = (
                        callee_knl.iname_to_tags[iname])
            new_domain = new_domain.set_dim_name(
                dim_type, i, iname_map[iname])
        new_domains.append(new_domain)

    kernel = kernel.copy(domains=kernel.domains + new_domains,
            iname_to_tags=new_iname_to_tags)

    # }}}

    # {{{ rename temporaries

    temp_map = {}
    new_temps = kernel.temporary_variables.copy()
    for name, temp in six.iteritems(callee_knl.temporary_variables):
        new_name = vng(callee_label+name)
        temp_map[name] = new_name
        new_temps[new_name] = temp.copy(name=new_name)

    kernel = kernel.copy(temporary_variables=new_temps)

    # }}}

    # {{{ match kernel arguments

    arg_map = {}  # callee arg name -> caller symbols (e.g. SubArrayRef)

    assignees = instruction.assignees  # writes
    parameters = instruction.expression.parameters  # reads

    # add keyword parameters
    from pymbolic.primitives import CallWithKwargs

    if isinstance(instruction.expression, CallWithKwargs):
        from loopy.kernel.function_interface import get_kw_pos_association

        _, pos_to_kw = get_kw_pos_association(callee_knl)
        kw_parameters = instruction.expression.kw_parameters
        for i in range(len(parameters), len(parameters) + len(kw_parameters)):
            parameters = parameters + (kw_parameters[pos_to_kw[i]],)

    assignee_pos = 0
    parameter_pos = 0
    for i, arg in enumerate(callee_knl.args):
        if arg.is_output_only:
            arg_map[arg.name] = assignees[assignee_pos]
            assignee_pos += 1
        else:
            arg_map[arg.name] = parameters[parameter_pos]
            parameter_pos += 1

    # }}}

    # {{{ rewrite instructions

    import pymbolic.primitives as p
    from pymbolic.mapper.substitutor import make_subst_func

    var_map = dict((p.Variable(k), p.Variable(v))
                   for k, v in six.iteritems(iname_map))
    var_map.update(dict((p.Variable(k), p.Variable(v))
                        for k, v in six.iteritems(temp_map)))
    var_map.update(dict((p.Variable(k), p.Variable(v.subscript.aggregate.name))
                        for k, v in six.iteritems(arg_map)))
    subst_mapper = KernelInliner(
        make_subst_func(var_map), kernel, arg_map, callee_knl.arg_dict)

    insn_id = {}
    for insn in callee_knl.instructions:
        insn_id[insn.id] = ing(callee_label+insn.id)

    # {{{ root and leave instructions in callee kernel

    dep_map = callee_knl.recursive_insn_dep_map()
    # roots depend on nothing
    heads = set(insn for insn, deps in six.iteritems(dep_map) if not deps)
    # leaves have nothing that depends on them
    tails = set(dep_map.keys())
    for insn, deps in six.iteritems(dep_map):
        tails = tails - deps

    # }}}

    # {{{ use NoOp to mark the start and end of callee kernel

    from loopy.kernel.instruction import NoOpInstruction

    noop_start = NoOpInstruction(
        id=ing(callee_label+"_start"),
        within_inames=instruction.within_inames,
        depends_on=instruction.depends_on
    )
    noop_end = NoOpInstruction(
        id=instruction.id,
        within_inames=instruction.within_inames,
        depends_on=frozenset(insn_id[insn] for insn in tails)
    )
    # }}}

    inner_insns = [noop_start]

    for insn in callee_knl.instructions:
        insn = insn.with_transformed_expressions(subst_mapper)
        within_inames = frozenset(map(iname_map.get, insn.within_inames))
        within_inames = within_inames | instruction.within_inames
        depends_on = frozenset(map(insn_id.get, insn.depends_on)) | (
                instruction.depends_on)
        if insn.id in heads:
            depends_on = depends_on | set([noop_start.id])
        insn = insn.copy(
            id=insn_id[insn.id],
            within_inames=within_inames,
            # TODO: probaby need to keep priority in callee kernel
            priority=instruction.priority,
            depends_on=depends_on
        )
        inner_insns.append(insn)

    inner_insns.append(noop_end)

    new_insns = []
    for insn in kernel.instructions:
        if insn == instruction:
            new_insns.extend(inner_insns)
        else:
            new_insns.append(insn)

    kernel = kernel.copy(instructions=new_insns)

    # }}}

    # {{{ transferring the scoped functions from callee to caller

    callee_scoped_calls_collector = CalleeScopedCallsCollector(
            callee_knl.scoped_functions)
    callee_scoped_calls_dict = {}

    for insn in kernel.instructions:
        if isinstance(insn, MultiAssignmentBase):
            callee_scoped_calls_dict.update(dict(callee_scoped_calls_collector(
                insn.expression)))
        elif isinstance(insn, (CInstruction, _DataObliviousInstruction)):
            pass
        else:
            raise NotImplementedError("Unknown type of instruction %s." % type(
                insn))

    kernel = change_names_of_pymbolic_calls(kernel,
            callee_scoped_calls_dict)

    # }}}

    return kernel

# }}}


# {{{ inline callable kernel

# FIXME This should take a 'within' parameter to be able to only inline
# *some* calls to a kernel, but not others.
def inline_callable_kernel(kernel, function_name):
    """
    Returns a copy of *kernel* with the callable kernel addressed by
    (scoped) name *function_name* inlined.
    """
    from loopy.preprocess import infer_arg_descr
    kernel = infer_arg_descr(kernel)

    old_insns = kernel.instructions
    for insn in old_insns:
        if isinstance(insn, CallInstruction):
            # FIXME This seems to use identifiers across namespaces. Why not
            # check whether the function is a scoped function first?
            if insn.expression.function.name in kernel.scoped_functions:
                in_knl_callable = kernel.scoped_functions[
                        insn.expression.function.name]
                from loopy.kernel.function_interface import CallableKernel
                if isinstance(in_knl_callable, CallableKernel) and (
                        in_knl_callable.subkernel.name == function_name):
                    kernel = _inline_call_instruction(
                            kernel, in_knl_callable.subkernel, insn)
        elif isinstance(insn, (MultiAssignmentBase, CInstruction,
                _DataObliviousInstruction)):
            pass
        else:
            raise NotImplementedError(
                    "Unknown instruction type %s"
                    % type(insn).__name__)

    return kernel

# }}}


# {{{ tools to match caller to callee args by (guessed) automatic reshaping

# (This is undocumented and not recommended, but it is currently needed
# to support Firedrake.)

class DimChanger(IdentityMapper):
    """
    Mapper to change the dimensions of an argument.

    .. attribute:: callee_arg_dict

        A mapping from the argument name (:class:`str`) to instances of
        :class:`loopy.kernel.array.ArrayBase`.

    .. attribute:: desried_shape

        A mapping from argument name (:class:`str`) to an instance of
        :class:`tuple`.
    """
    def __init__(self, callee_arg_dict, desired_shape):
        self.callee_arg_dict = callee_arg_dict
        self.desired_shape = desired_shape

    def map_subscript(self, expr):
        callee_arg_dim_tags = self.callee_arg_dict[expr.aggregate.name].dim_tags
        flattened_index = sum(dim_tag.stride*idx for dim_tag, idx in
                zip(callee_arg_dim_tags, expr.index_tuple))
        new_indices = []

        from operator import mul
        from functools import reduce
        stride = reduce(mul, self.desired_shape[expr.aggregate.name], 1)

        for length in self.desired_shape[expr.aggregate.name]:
            stride /= length
            ind = flattened_index // int(stride)
            flattened_index -= (int(stride) * ind)
            new_indices.append(simplify_via_aff(ind))

        return expr.aggregate.index(tuple(new_indices))


def _match_caller_callee_argument_dimension(caller_knl, callee_function_name):
    """
    Returns a copy of *caller_knl* with the instance of
    :class:`loopy.kernel.function_interface.CallableKernel` addressed by
    *callee_function_name* in the *caller_knl* aligned with the argument
    dimesnsions required by *caller_knl*.
    """
    pymbolic_calls_to_new_callables = {}
    for insn in caller_knl.instructions:
        if not isinstance(insn, CallInstruction) or (
                insn.expression.function.name not in
                caller_knl.scoped_functions):
            # Call to a callable kernel can only occur through a
            # CallInstruction.
            continue

        in_knl_callable = caller_knl.scoped_functions[
                insn.expression.function.name]

        if in_knl_callable.subkernel.name != callee_function_name:
            # Not the callable we're looking for.
            continue

        # getting the caller->callee arg association

        parameters = insn.expression.parameters[:]
        kw_parameters = {}
        if isinstance(insn.expression, CallWithKwargs):
            kw_parameters = insn.expression.kw_parameters

        assignees = insn.assignees

        parameter_shapes = [par.get_array_arg_descriptor(caller_knl).shape
                for par in parameters]
        kw_to_pos, pos_to_kw = get_kw_pos_association(in_knl_callable.subkernel)
        for i in range(len(parameters), len(parameters)+len(kw_parameters)):
            parameter_shapes.append(kw_parameters[pos_to_kw[i]]
                    .get_array_arg_descriptor(caller_knl).shape)

        # inserting the assigness at the required positions.
        assignee_write_count = -1
        for i, arg in enumerate(in_knl_callable.subkernel.args):
            if arg.is_output_only:
                assignee = assignees[-assignee_write_count-1]
                parameter_shapes.insert(i, assignee
                        .get_array_arg_descriptor(caller_knl).shape)
                assignee_write_count -= 1

        callee_arg_to_desired_dim_tag = dict(zip([arg.name for arg in
            in_knl_callable.subkernel.args], parameter_shapes))
        dim_changer = DimChanger(in_knl_callable.subkernel.arg_dict,
                callee_arg_to_desired_dim_tag)
        new_callee_insns = []
        for callee_insn in in_knl_callable.subkernel.instructions:
            if isinstance(callee_insn, MultiAssignmentBase):
                new_callee_insns.append(callee_insn.copy(expression=dim_changer(
                    callee_insn.expression),
                    assignee=dim_changer(callee_insn.assignee)))
            elif isinstance(callee_insn, (CInstruction,
                    _DataObliviousInstruction)):
                pass
            else:
                raise NotImplementedError("Unknwon instruction %s." %
                        type(insn))

        # subkernel with instructions adjusted according to the new dimensions.
        new_subkernel = in_knl_callable.subkernel.copy(instructions=new_callee_insns)

        new_in_knl_callable = in_knl_callable.copy(subkernel=new_subkernel)

        pymbolic_calls_to_new_callables[insn.expression] = new_in_knl_callable

    if not pymbolic_calls_to_new_callables:
        # complain if no matching function found.
        raise LoopyError("No CallableKernel with the name %s found in %s." % (
            callee_function_name, caller_knl.name))

    return change_names_of_pymbolic_calls(caller_knl,
            pymbolic_calls_to_new_callables)

# }}}


# vim: foldmethod=marker