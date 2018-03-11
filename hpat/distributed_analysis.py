from __future__ import print_function, division, absolute_import
from collections import namedtuple
import copy

import numba
from numba import ir, ir_utils, types
from numba.ir_utils import (get_call_table, get_tuple_table, find_topo_order,
                            guard, get_definition, require, find_callname,
                            mk_unique_var, compile_to_numba_ir, replace_arg_nodes)
from numba.parfor import Parfor
from numba.parfor import wrap_parfor_blocks, unwrap_parfor_blocks

import numpy as np
import hpat
from hpat.utils import (get_definitions, is_alloc_call, is_whole_slice,
                        update_node_definitions, is_array, is_np_array)

from enum import Enum


class Distribution(Enum):
    REP = 1
    Thread = 2
    TwoD = 3
    OneD_Var = 4
    OneD = 5


_dist_analysis_result = namedtuple(
    'dist_analysis_result', 'array_dists,parfor_dists')

distributed_analysis_extensions = {}


class DistributedAnalysis(object):
    """analyze program for to distributed transfromation"""

    def __init__(self, func_ir, typemap, calltypes, typingctx):
        self.func_ir = func_ir
        self.typemap = typemap
        self.calltypes = calltypes
        self.typingctx = typingctx

    def _init_run(self):
        self._call_table, _ = get_call_table(self.func_ir.blocks)
        self._tuple_table = get_tuple_table(self.func_ir.blocks)
        self._parallel_accesses = set()
        self._T_arrs = set()
        self.second_pass = False
        self.in_parallel_parfor = -1

    def run(self):
        self._init_run()
        blocks = self.func_ir.blocks
        array_dists = {}
        parfor_dists = {}
        topo_order = find_topo_order(blocks)
        self._run_analysis(self.func_ir.blocks, topo_order,
                           array_dists, parfor_dists)
        self.second_pass = True
        self._run_analysis(self.func_ir.blocks, topo_order,
                           array_dists, parfor_dists)
        # rebalance arrays if necessary
        if Distribution.OneD_Var in array_dists.values():
            changed = self._rebalance_arrs(array_dists, parfor_dists)
            if changed:
                return self.run()

        return _dist_analysis_result(array_dists=array_dists, parfor_dists=parfor_dists)

    def _run_analysis(self, blocks, topo_order, array_dists, parfor_dists):
        save_array_dists = {}
        save_parfor_dists = {1: 1}  # dummy value
        # fixed-point iteration
        while array_dists != save_array_dists or parfor_dists != save_parfor_dists:
            save_array_dists = copy.copy(array_dists)
            save_parfor_dists = copy.copy(parfor_dists)
            for label in topo_order:
                self._analyze_block(blocks[label], array_dists, parfor_dists)

    def _analyze_block(self, block, array_dists, parfor_dists):
        for inst in block.body:
            if isinstance(inst, ir.Assign):
                self._analyze_assign(inst, array_dists, parfor_dists)
            elif isinstance(inst, Parfor):
                self._analyze_parfor(inst, array_dists, parfor_dists)
            elif isinstance(inst, (ir.SetItem, ir.StaticSetItem)):
                self._analyze_setitem(inst, array_dists)
            elif type(inst) in distributed_analysis_extensions:
                # let external calls handle stmt if type matches
                f = distributed_analysis_extensions[type(inst)]
                f(inst, array_dists)
            else:
                self._set_REP(inst.list_vars(), array_dists)

    def _analyze_assign(self, inst, array_dists, parfor_dists):
        lhs = inst.target.name
        rhs = inst.value
        # treat return casts like assignments
        if isinstance(rhs, ir.Expr) and rhs.op == 'cast':
            rhs = rhs.value

        if isinstance(rhs, ir.Var) and is_array(self.typemap, lhs):
            self._meet_array_dists(lhs, rhs.name, array_dists)
            return
        elif is_array(self.typemap, lhs) and isinstance(rhs, ir.Expr) and rhs.op == 'inplace_binop':
            # distributions of all 3 variables should meet (lhs, arg1, arg2)
            arg1 = rhs.lhs.name
            arg2 = rhs.rhs.name
            dist = self._meet_array_dists(arg1, arg2, array_dists)
            dist = self._meet_array_dists(arg1, lhs, array_dists, dist)
            self._meet_array_dists(arg1, arg2, array_dists, dist)
            return
        elif isinstance(rhs, ir.Expr) and rhs.op in ['getitem', 'static_getitem']:
            self._analyze_getitem(inst, lhs, rhs, array_dists)
            return
        elif isinstance(rhs, ir.Expr) and rhs.op == 'build_tuple':
            # parallel arrays can be packed and unpacked from tuples
            # e.g. boolean array index in test_getitem_multidim
            return
        elif (isinstance(rhs, ir.Expr) and rhs.op == 'getattr' and rhs.attr == 'T'
              and is_array(self.typemap, lhs)):
            # array and its transpose have same distributions
            arr = rhs.value.name
            self._meet_array_dists(lhs, arr, array_dists)
            # keep lhs in table for dot() handling
            self._T_arrs.add(lhs)
            return
        elif (isinstance(rhs, ir.Expr) and rhs.op == 'getattr'
                and rhs.attr in ['shape', 'ndim', 'size', 'strides', 'dtype',
                                 'itemsize', 'astype', 'reshape']):
            pass  # X.shape doesn't affect X distribution
        elif isinstance(rhs, ir.Expr) and rhs.op == 'call':
            self._analyze_call(lhs, rhs.func.name, rhs.args, array_dists)
        else:
            self._set_REP(inst.list_vars(), array_dists)
        return

    def _analyze_parfor(self, parfor, array_dists, parfor_dists):
        if parfor.id not in parfor_dists:
            parfor_dists[parfor.id] = Distribution.OneD

        # analyze init block first to see array definitions
        self._analyze_block(parfor.init_block, array_dists, parfor_dists)
        out_dist = Distribution.OneD
        if self.in_parallel_parfor != -1:
            out_dist = Distribution.REP

        parfor_arrs = set()  # arrays this parfor accesses in parallel
        array_accesses = ir_utils.get_array_accesses(parfor.loop_body)
        par_index_var = parfor.loop_nests[0].index_variable.name
        stencil_accesses, _ = get_stencil_accesses(parfor, self.typemap)
        for (arr, index) in array_accesses:
            if index == par_index_var or index in stencil_accesses:
                parfor_arrs.add(arr)
                self._parallel_accesses.add((arr, index))
            if index in self._tuple_table:
                index_tuple = [(var.name if isinstance(var, ir.Var) else var)
                               for var in self._tuple_table[index]]
                if index_tuple[0] == par_index_var:
                    parfor_arrs.add(arr)
                    self._parallel_accesses.add((arr, index))
                if par_index_var in index_tuple[1:]:
                    out_dist = Distribution.REP
            # TODO: check for index dependency

        for arr in parfor_arrs:
            if arr in array_dists:
                out_dist = Distribution(
                    min(out_dist.value, array_dists[arr].value))
        parfor_dists[parfor.id] = out_dist
        for arr in parfor_arrs:
            if arr in array_dists:
                array_dists[arr] = out_dist

        # TODO: find prange actually coming from user
        # for pattern in parfor.patterns:
        #     if pattern[0] == 'prange' and not self.in_parallel_parfor:
        #         parfor_dists[parfor.id] = Distribution.OneD

        # run analysis recursively on parfor body
        if self.second_pass and out_dist in [Distribution.OneD,
                                             Distribution.OneD_Var]:
            self.in_parallel_parfor = parfor.id
        blocks = wrap_parfor_blocks(parfor)
        for b in blocks.values():
            self._analyze_block(b, array_dists, parfor_dists)
        unwrap_parfor_blocks(parfor)
        if self.in_parallel_parfor == parfor.id:
            self.in_parallel_parfor = -1
        return

    def _analyze_call(self, lhs, func_var, args, array_dists):
        if func_var not in self._call_table or not self._call_table[func_var]:
            self._analyze_call_set_REP(lhs, func_var, args, array_dists)
            return

        call_list = self._call_table[func_var]

        if is_alloc_call(func_var, self._call_table):
            if lhs not in array_dists:
                array_dists[lhs] = Distribution.OneD
            return

        if self._is_call(func_var, [len]):
            return

        if call_list == ['astype'] or call_list == ['reshape']:
            call_def = guard(get_definition, self.func_ir, func_var)
            if (isinstance(call_def, ir.Expr) and call_def.op == 'getattr'
                    and is_array(self.typemap, call_def.value.name)):
                in_arr_name = call_def.value.name
                self._meet_array_dists(lhs, in_arr_name, array_dists)
                return

        if self._is_call(func_var, ['ravel', np]):
            self._meet_array_dists(lhs, args[0].name, array_dists)
            return

        if hpat.config._has_h5py and (self._is_call(func_var, ['h5read', hpat.pio_api])
                                      or self._is_call(func_var, ['h5write', hpat.pio_api])):
            return

        if call_list == ['quantile', 'hiframes_api', hpat]:
            # quantile doesn't affect input's distribution
            return

        if call_list == ['dist_return', 'distributed_api', hpat]:
            arr_name = args[0].name
            assert arr_name in array_dists, "array distribution not found"
            if array_dists[arr_name] == Distribution.REP:
                raise ValueError("distributed return of array {} not valid"
                                 " since it is replicated")
            return

        if call_list == ['dist_input', 'distributed_api', hpat]:
            if lhs not in array_dists:
                array_dists[lhs] = Distribution.OneD_Var
            return

        if call_list == ['threaded_input', 'distributed_api', hpat]:
            if lhs not in array_dists:
                array_dists[lhs] = Distribution.Thread
            return

        if call_list == ['rebalance_array', 'distributed_api', hpat]:
            if lhs not in array_dists:
                array_dists[lhs] = Distribution.OneD
            in_arr = args[0].name
            if array_dists[in_arr] == Distribution.OneD_Var:
                array_dists[lhs] = Distribution.OneD
            else:
                self._meet_array_dists(lhs, in_arr, array_dists)
            return

        if hpat.config._has_ros and call_list == ['read_ros_images_inner', 'ros', hpat]:
            return

        if hpat.config._has_pyarrow and call_list == [hpat.parquet_pio.read_parquet]:
            return

        if hpat.config._has_pyarrow and call_list == [hpat.parquet_pio.read_parquet_str]:
            # string read creates array in output
            if lhs not in array_dists:
                array_dists[lhs] = Distribution.OneD
            return

        if (len(call_list) == 2 and call_list[1] == np
                and call_list[0] in ['cumsum', 'cumprod', 'empty_like',
                                     'zeros_like', 'ones_like', 'full_like', 'copy']):
            in_arr = args[0].name
            self._meet_array_dists(lhs, in_arr, array_dists)
            return

        if call_list == ['concatenate', np]:
            self._analyze_call_np_concatenate(lhs, args, array_dists)
            return

        # sum over the first axis is distributed, A.sum(0)
        if call_list == ['sum', np] and len(args) == 2:
            axis_def = guard(get_definition, self.func_ir, args[1])
            if isinstance(axis_def, ir.Const) and axis_def.value == 0:
                array_dists[lhs] = Distribution.REP
                return

        if self._is_call(func_var, ['dot', np]):
            self._analyze_call_np_dot(lhs, args, array_dists)
            return

        if call_list == ['train']:
            getattr_call = guard(get_definition, self.func_ir, func_var)
            if getattr_call and self.typemap[getattr_call.value.name] == hpat.ml.svc.svc_type:
                self._meet_array_dists(
                    args[0].name, args[1].name, array_dists, Distribution.Thread)
                return
            if getattr_call and self.typemap[getattr_call.value.name] == hpat.ml.naive_bayes.mnb_type:
                self._meet_array_dists(args[0].name, args[1].name, array_dists)
                return

        if call_list == ['predict']:
            getattr_call = guard(get_definition, self.func_ir, func_var)
            if getattr_call and self.typemap[getattr_call.value.name] == hpat.ml.svc.svc_type:
                self._meet_array_dists(
                    lhs, args[0].name, array_dists, Distribution.Thread)
                return
            if getattr_call and self.typemap[getattr_call.value.name] == hpat.ml.naive_bayes.mnb_type:
                self._meet_array_dists(lhs, args[0].name, array_dists)
                return

        # set REP if not found
        self._analyze_call_set_REP(lhs, func_var, args, array_dists)

    def _analyze_call_np_concatenate(self, lhs, args, array_dists):
        assert len(args) == 1
        tup_def = guard(get_definition, self.func_ir, args[0])
        assert isinstance(tup_def, ir.Expr) and tup_def.op == 'build_tuple'
        in_arrs = tup_def.items
        # input arrays have same distribution
        in_dist = Distribution.OneD
        for v in in_arrs:
            in_dist = Distribution(
                min(in_dist.value, array_dists[v.name].value))
        # OneD_Var since sum of block sizes might not be exactly 1D
        out_dist = Distribution.OneD_Var
        out_dist = Distribution(min(out_dist.value, in_dist.value))
        array_dists[lhs] = out_dist
        # output can cause input REP
        if out_dist != Distribution.OneD_Var:
            in_dist = out_dist
        for v in in_arrs:
            array_dists[v.name] = in_dist
        return

    def _analyze_call_np_dot(self, lhs, args, array_dists):

        arg0 = args[0].name
        arg1 = args[1].name
        ndim0 = self.typemap[arg0].ndim
        ndim1 = self.typemap[arg1].ndim
        dist0 = array_dists[arg0]
        dist1 = array_dists[arg1]
        # Fortran layout is caused by X.T and means transpose
        t0 = arg0 in self._T_arrs
        t1 = arg1 in self._T_arrs
        if ndim0 == 1 and ndim1 == 1:
            # vector dot, both vectors should have same layout
            new_dist = Distribution(min(array_dists[arg0].value,
                                        array_dists[arg1].value))
            array_dists[arg0] = new_dist
            array_dists[arg1] = new_dist
            return
        if ndim0 == 2 and ndim1 == 1 and not t0:
            # special case were arg1 vector is treated as column vector
            # samples dot weights: np.dot(X,w)
            # w is always REP
            array_dists[arg1] = Distribution.REP
            if lhs not in array_dists:
                array_dists[lhs] = Distribution.OneD
            # lhs and X have same distribution
            self._meet_array_dists(lhs, arg0, array_dists)
            dprint("dot case 1 Xw:", arg0, arg1)
            return
        if ndim0 == 1 and ndim1 == 2 and not t1:
            # reduction across samples np.dot(Y,X)
            # lhs is always REP
            array_dists[lhs] = Distribution.REP
            # Y and X have same distribution
            self._meet_array_dists(arg0, arg1, array_dists)
            dprint("dot case 2 YX:", arg0, arg1)
            return
        if ndim0 == 2 and ndim1 == 2 and t0 and not t1:
            # reduction across samples np.dot(X.T,Y)
            # lhs is always REP
            array_dists[lhs] = Distribution.REP
            # Y and X have same distribution
            self._meet_array_dists(arg0, arg1, array_dists)
            dprint("dot case 3 XtY:", arg0, arg1)
            return
        if ndim0 == 2 and ndim1 == 2 and not t0 and not t1:
            # samples dot weights: np.dot(X,w)
            # w is always REP
            array_dists[arg1] = Distribution.REP
            self._meet_array_dists(lhs, arg0, array_dists)
            dprint("dot case 4 Xw:", arg0, arg1)
            return

        # set REP if no pattern matched
        self._analyze_call_set_REP(lhs, func_var, args, array_dists)

    def _analyze_call_set_REP(self, lhs, func_var, args, array_dists):
        for v in args:
            if is_array(self.typemap, v.name):
                dprint("dist setting call arg REP {}".format(v.name))
                array_dists[v.name] = Distribution.REP
        if is_array(self.typemap, lhs):
            dprint("dist setting call out REP {}".format(lhs))
            array_dists[lhs] = Distribution.REP

    def _analyze_getitem(self, inst, lhs, rhs, array_dists):
        if rhs.op == 'static_getitem':
            if rhs.index_var is None:
                # TODO: things like A[0] need broadcast
                self._set_REP(inst.list_vars(), array_dists)
                return
            index_var = rhs.index_var
        else:
            assert rhs.op == 'getitem'
            index_var = rhs.index

        if (rhs.value.name, index_var.name) in self._parallel_accesses:
            # XXX: is this always valid? should be done second pass?
            self._set_REP([inst.target], array_dists)
            return

        # in multi-dimensional case, we only consider first dimension
        # TODO: extend to 2D distribution
        if index_var.name in self._tuple_table:
            inds = self._tuple_table[index_var.name]
            index_var = inds[0]
            # rest of indices should be replicated if array
            other_ind_vars = [v for v in inds[1:] if isinstance(v, ir.Var)]
            self._set_REP(other_ind_vars, array_dists)

        if isinstance(index_var, int):
            self._set_REP(inst.list_vars(), array_dists)
            return
        assert isinstance(index_var, ir.Var)

        # array selection with boolean index
        if (is_np_array(self.typemap, index_var.name)
                and self.typemap[index_var.name].dtype == types.boolean):
            # input array and bool index have the same distribution
            new_dist = self._meet_array_dists(index_var.name, rhs.value.name,
                                              array_dists)
            array_dists[lhs] = Distribution(min(Distribution.OneD_Var.value,
                                                new_dist.value))
            return

        if guard(is_whole_slice, self.typemap, self.func_ir, index_var):
            # for example: A = X[:,3]
            self._meet_array_dists(lhs, rhs.value.name, array_dists)
            return

        self._set_REP(inst.list_vars(), array_dists)
        return

    def _analyze_setitem(self, inst, array_dists):
        if isinstance(inst, ir.SetItem):
            index_var = inst.index
        else:
            index_var = inst.index_var

        if ((inst.target.name, index_var.name) in self._parallel_accesses):
            # no parallel to parallel array set (TODO)
            return

        if index_var.name in self._tuple_table:
            inds = self._tuple_table[index_var.name]
            index_var = inds[0]
            # rest of indices should be replicated if array
            self._set_REP(inds[1:], array_dists)

        if guard(is_whole_slice, self.typemap, self.func_ir, index_var):
            # for example: X[:,3] = A
            self._meet_array_dists(
                inst.target.name, inst.value.name, array_dists)
            return

        self._set_REP([inst.value], array_dists)

    def _meet_array_dists(self, arr1, arr2, array_dists, top_dist=None):
        if top_dist is None:
            top_dist = Distribution.OneD
        if arr1 not in array_dists:
            array_dists[arr1] = top_dist
        if arr2 not in array_dists:
            array_dists[arr2] = top_dist

        new_dist = Distribution(min(array_dists[arr1].value,
                                    array_dists[arr2].value))
        new_dist = Distribution(min(new_dist.value, top_dist.value))
        array_dists[arr1] = new_dist
        array_dists[arr2] = new_dist
        return new_dist

    def _set_REP(self, var_list, array_dists):
        for var in var_list:
            varname = var.name
            if is_array(self.typemap, varname):
                dprint("dist setting REP {}".format(varname))
                array_dists[varname] = Distribution.REP
            # handle tuples of arrays
            var_def = guard(get_definition, self.func_ir, var)
            if (var_def is not None and isinstance(var_def, ir.Expr)
                    and var_def.op == 'build_tuple'):
                tuple_vars = var_def.items
                self._set_REP(tuple_vars, array_dists)

    def _is_call(self, func_var, call_list):
        if func_var not in self._call_table:
            return False
        return self._call_table[func_var] == call_list

    def _rebalance_arrs(self, array_dists, parfor_dists):
        # rebalance an array if it is accessed in a parfor that has output
        # arrays or is in a loop

        # find sequential loop bodies
        cfg = numba.analysis.compute_cfg_from_blocks(self.func_ir.blocks)
        loop_bodies = set()
        for loop in cfg.loops().values():
            loop_bodies |= loop.body

        rebalance_arrs = set()

        for label, block in self.func_ir.blocks.items():
            for inst in block.body:
                if (isinstance(inst, Parfor)
                        and parfor_dists[inst.id] == Distribution.OneD_Var):
                    array_accesses = ir_utils.get_array_accesses(inst.loop_body)
                    onedv_arrs = set(arr for (arr, ind) in array_accesses
                                 if arr in array_dists and array_dists[arr] == Distribution.OneD_Var)
                    if (label in loop_bodies
                            or _arrays_written(onedv_arrs, inst.loop_body)):
                        rebalance_arrs |= onedv_arrs

        if len(rebalance_arrs) != 0:
            self._gen_rebalances(rebalance_arrs, self.func_ir.blocks)
            return True

        return False

    def _gen_rebalances(self, rebalance_arrs, blocks):
        #
        for block in blocks.values():
            new_body = []
            for inst in block.body:
                if isinstance(inst, Parfor):
                    self._gen_rebalances(rebalance_arrs, {0: inst.init_block})
                    self._gen_rebalances(rebalance_arrs, inst.loop_body)
                if isinstance(inst, ir.Assign) and inst.target.name in rebalance_arrs:
                    out_arr = inst.target
                    self.func_ir._definitions[out_arr.name].remove(inst.value)
                    # hold inst results in tmp array
                    tmp_arr = ir.Var(out_arr.scope,
                                     mk_unique_var("rebalance_tmp"),
                                     out_arr.loc)
                    self.typemap[tmp_arr.name] = self.typemap[out_arr.name]
                    inst.target = tmp_arr
                    nodes = [inst]

                    def f(in_arr):  # pragma: no cover
                        out_a = hpat.distributed_api.rebalance_array(in_arr)
                    f_block = compile_to_numba_ir(f, {'hpat': hpat}, self.typingctx,
                                                  (self.typemap[tmp_arr.name],),
                                                  self.typemap, self.calltypes).blocks.popitem()[1]
                    replace_arg_nodes(f_block, [tmp_arr])
                    nodes += f_block.body[:-3]  # remove none return
                    nodes[-1].target = out_arr
                    update_node_definitions(nodes, self.func_ir._definitions)
                    new_body += nodes
                else:
                    new_body.append(inst)

            block.body = new_body


def _arrays_written(arrs, blocks):
    for block in blocks.values():
        for inst in block.body:
            if isinstance(inst, Parfor) and _arrays_written(arrs, inst.loop_body):
                return True
            if (isinstance(inst, (ir.SetItem, ir.StaticSetItem))
                    and inst.target.name in arrs):
                return True
    return False

def get_stencil_accesses(parfor, typemap):
    # if a parfor has stencil pattern, see which accesses depend on loop index
    # XXX: assuming loop index is not used for non-stencil arrays
    # TODO support recursive parfor, multi-D, mutiple body blocks

    # no access if not stencil
    is_stencil = False
    for pattern in parfor.patterns:
        if pattern[0] == 'stencil':
            is_stencil = True
            neighborhood = pattern[1]
    if not is_stencil:
        return {}, None

    par_index_var = parfor.loop_nests[0].index_variable
    body = parfor.loop_body
    body_defs = get_definitions(body)

    stencil_accesses = {}

    for block in body.values():
        for stmt in block.body:
            if isinstance(stmt, ir.Assign) and isinstance(stmt.value, ir.Expr):
                lhs = stmt.target.name
                rhs = stmt.value
                if (rhs.op == 'getitem' and is_array(typemap, rhs.value.name)
                        and vars_dependent(body_defs, rhs.index, par_index_var)):
                    stencil_accesses[rhs.index.name] = rhs.value.name

    return stencil_accesses, neighborhood


def vars_dependent(defs, var1, var2):
    # see if var1 depends on var2 based on definitions in defs
    if len(defs[var1.name]) != 1:
        return False

    vardef = defs[var1.name][0]
    if isinstance(vardef, ir.Var) and vardef.name == var2.name:
        return True
    if isinstance(vardef, ir.Expr):
        for invar in vardef.list_vars():
            if invar.name == var2.name or vars_dependent(defs, invar, var2):
                return True
    return False


def dprint(*s):
    if numba.config.DEBUG_ARRAY_OPT == 1:
        print(*s)
