#!/bin/python3
#
# Reverse : reverse engineering for x86 binaries
# Copyright (C) 2015    Joel
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.    See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.    If not, see <http://www.gnu.org/licenses/>.
#

import time

import lib.colors
from lib.ast import (Ast_Branch, Ast_Comment, Ast_Jmp, Ast_Loop, 
        Ast_IfGoto, Ast_Ifelse, Ast_AndIf, assign_colors, search_local_vars,
        fuse_inst_with_if, search_canary_plt)
from lib.utils import (invert_cond, BRANCH_NEXT, BRANCH_NEXT_JUMP, die,
        debug__)
from lib.paths import get_loop_start


gph = None
print_andif = True


def get_ast_ifgoto(paths, curr_loop_idx, inst):
    nxt = gph.link_out[inst.address]

    c1 = paths.loop_contains(curr_loop_idx, nxt[BRANCH_NEXT])
    c2 = paths.loop_contains(curr_loop_idx, nxt[BRANCH_NEXT_JUMP])

    if c1 and c2:
        die("can't have a ifelse here     %x" % inst.address)

    # If the address of the jump is inside the loop, we
    # invert the conditions. example :
    #
    # jmp conditions
    # loop:
    #    code ...
    # conditions:
    #    cmp ...
    #    jg endloop
    #    cmp ...
    #    jne loop
    # endloop:
    #
    # Here the last jump point inside the loop. We want to
    # replace by this : 
    #
    # loop {
    #    cmp ...
    #    jg endloop
    #    cmp ...
    #    je endloop
    #    code ...
    # } # here there is an implicit jmp to loop
    # endloop:
    #

    cond_id = inst.id
    br = nxt[BRANCH_NEXT_JUMP]
    if c2:
        cond_id = invert_cond(cond_id)
        br = nxt[BRANCH_NEXT]

    return Ast_IfGoto(inst, cond_id, br)


def get_ast_branch(paths, curr_loop_idx=[], last_else=-1, endif=-1):
    ast = Ast_Branch()
    if_printed = False

    if paths.rm_empty_paths():
        return ast

    while 1:
        # Stop at the first split or loop
        nb_commons, is_loop, is_ifelse, force_stop_addr = \
            paths.head_last_common(curr_loop_idx)

        if nb_commons > 0:
            common_path = paths.pop(nb_commons)

            for ad in common_path:
                blk = gph.nodes[ad]

                # Here if we have conditional jump, it's not a ifelse,
                # it's a condition for a loop. It will be replaced by a
                # goto. ifgoto are skipped by head_last_common.
                if ad in gph.cond_jumps_set:
                    inst = blk[0] # first inst
                    ast.add(get_ast_ifgoto(paths, curr_loop_idx, inst))
                else:
                    ast.add(blk)

            if paths.rm_empty_paths():
                return ast

        if force_stop_addr != 0:
            ad = paths.first()
            blk = gph.nodes[ad]
            ast.add(blk)

            if ad not in gph.uncond_jumps_set:
                ast.add(Ast_Jmp(gph.link_out[blk[0].address][BRANCH_NEXT]))
            break

        if is_loop:
            # last_else == -1
            # -> we can't go to a same else inside a loop
            a, endpoint = get_ast_loop(paths, curr_loop_idx, -1, endif)
            ast.add(a)
        elif is_ifelse:
            a, endpoint = get_ast_ifelse(paths, curr_loop_idx, last_else, if_printed, endif)
            if_printed = isinstance(a, Ast_Ifelse)
            ast.add(a)
        else:
            endpoint = paths.first()

        if endpoint == -1 or paths.goto_addr(endpoint):
            break

    return ast


# TODO move in class Paths
# Assume that the beginning of paths is the beginning of a loop
def paths_is_infinite(paths):
    for k, p in paths.paths.items():
        for addr in p:
            if addr in gph.cond_jumps_set:
                nxt = gph.link_out[addr]
                if nxt[BRANCH_NEXT] not in paths or \
                   nxt[BRANCH_NEXT_JUMP] not in paths: \
                    return False
    return True


def get_ast_loop(paths, last_loop_idx, last_else, endif):
    ast = Ast_Loop()
    curr_loop_idx = paths.get_loops_idx()
    first_blk = gph.nodes[get_loop_start(curr_loop_idx)]

    if first_blk[0].address in gph.cond_jumps_set:
        ast.add(get_ast_ifgoto(paths, curr_loop_idx, first_blk[0]))
    else:
        ast.add(first_blk)

    loop_paths, endloops, endloops_start = \
        paths.extract_loop_paths(curr_loop_idx, last_loop_idx, endif)

    # Checking if endloop == [] to determine if it's an 
    # infinite loop is not sufficient
    # tests/nestedloop2
    ast.set_infinite(paths_is_infinite(loop_paths))

    loop_paths.pop(1)
    ast.add(get_ast_branch(loop_paths, curr_loop_idx, last_else))

    if not endloops:
        return ast, -1

    epilog = Ast_Branch()
    if len(endloops) > 1:
        i = 1
        for el in endloops[:-1]:
            if el.first() in endloops_start:
                epilog.add(Ast_Comment("endloop " + str(i)))
                i += 1
            epilog.add(get_ast_branch(el, last_loop_idx, last_else))
        epilog.add(Ast_Comment("endloop " + str(i)))

        ast.set_epilog(epilog)

    return ast, endloops[-1].first()


def get_ast_ifelse(paths, curr_loop_idx, last_else, is_prev_andif, endif):
    addr = paths.pop(1)[0]
    paths.rm_empty_paths()
    jump_inst = gph.nodes[addr][0]
    nxt = gph.link_out[addr]

    if_addr = nxt[BRANCH_NEXT]
    else_addr = nxt[BRANCH_NEXT_JUMP] if len(nxt) == 2 else -1

    # If endpoint == -1, it means we are in a sub-if and the endpoint 
    # is after. When we create_split, only address inside current
    # if and else are kept.
    endpoint = paths.first_common_ifelse(curr_loop_idx, else_addr)
    split, else_addr = paths.split(addr, endpoint)

    # is_prev_and_if : better output (tests/if5)
    #
    # example C file :
    #
    # if 1 {
    #   if 2 { 
    #     ...
    #   }
    #   if 3 {
    #     ...
    #   }
    # }
    #
    #
    # output without the is_prev_andif. This is correct, the andif is 
    # attached to the "if 1", but it's not very clear.
    #
    # if 1 {
    #   if 2 { 
    #     ...
    #   }
    #   and if 3
    #   ...
    # }
    #
    # output with the is_prev_andif :
    # Instead of the andif, we have the same code as the original.
    #

    # last_else allows to not repeat the else part when there are some 
    # and in the If. example :
    #
    # if (i > 0 && i == 1) {
    #     part 1
    # } else {
    #     part 2
    # }
    #
    #
    # output without this "optimization" :
    #
    # ...
    # if > {
    #     ...
    #     if == {
    #         part 1
    #     } else != {
    #         part 2
    #     }
    # } else <= {
    #     part 2
    # }
    # 
    #
    # output with "optimization" :
    #
    # ...
    # if > {
    #     ...
    #     and if ==    means that if the condition is false, goto else
    #     part 1
    # } else <= {
    #     part 2
    # }
    #

    if print_andif:
        if last_else != -1 and not is_prev_andif:
            # TODO not sure about endpoint == -1
            # tests/break3
            if if_addr == last_else and endpoint == -1:
                return (Ast_AndIf(jump_inst, jump_inst.id), else_addr)

            # if else_addr == -1 or else_addr == last_else:
            if else_addr != -1 and (else_addr == last_else or else_addr == endif) or \
                    last_else == endif and endif == endpoint and endpoint != -1:
                endpoint = gph.link_out[addr][BRANCH_NEXT]
                return (Ast_AndIf(jump_inst, invert_cond(jump_inst.id)), endpoint)

    if else_addr == -1:
        else_addr = last_else

    if endpoint == -1:
        endpoint = endif

    a1 = get_ast_branch(split[BRANCH_NEXT_JUMP], curr_loop_idx, -1, endpoint)
    a2 = get_ast_branch(split[BRANCH_NEXT], curr_loop_idx, else_addr, endpoint)

    return (Ast_Ifelse(jump_inst, a1, a2), endpoint)



def generate_ast(graph):
    global gph
    gph = graph

    start = time.clock()

    ast = get_ast_branch(gph.paths)

    elapsed = time.clock()
    elapsed = elapsed - start
    debug__("Ast generated in %fs" % elapsed)

    # Process ast

    start = time.clock()

    search_local_vars(ast)
    fuse_inst_with_if(ast)
    search_canary_plt() 

    elapsed = time.clock()
    elapsed = elapsed - start
    debug__("Functions for processing ast in %fs" % elapsed)

    if not lib.colors.nocolor:
        assign_colors(ast)

    return ast
