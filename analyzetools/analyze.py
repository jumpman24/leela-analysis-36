import os
import pickle
import sys

import config
from sgftools import annotations, sgflib


def do_analyze(leela, base_dir, verbosity, seconds_per_search):
    ckpt_hash = 'analyze_' + leela.history_hash() + "_" + str(seconds_per_search) + "sec"
    ckpt_fn = os.path.join(base_dir, ckpt_hash)
    # if verbosity > 2:
    #     print("Looking for checkpoint file: %s" % ckpt_fn, file=sys.stderr)

    if os.path.exists(ckpt_fn) and not config.skip_checkpoints:
        skipped = True
        if verbosity > 1:
            print("Loading checkpoint file: %s" % ckpt_fn, file=sys.stderr)
        with open(ckpt_fn, 'rb') as ckpt_file:
            stats, move_list = pickle.load(ckpt_file)
            ckpt_file.close()
    else:
        skipped = False
        leela.reset()
        leela.go_to_position()
        stats, move_list = leela.analyze(seconds_per_search)
        with open(ckpt_fn, 'wb') as ckpt_file:
            pickle.dump((stats, move_list), ckpt_file)
            ckpt_file.close()

    return stats, move_list, skipped


# move_list is from a call to do_analyze
# Iteratively expands a tree of moves by expanding on the leaf with the highest "probability of reaching".
def do_variations(cursor, leela, stats, move_list, board_size, game_move, base_dir, args):
    nodes_per_variation = args.nodes_per_variation
    verbosity = args.verbosity

    if 'bookmoves' in stats or len(move_list) <= 0:
        return None

    rootcolor = leela.whose_turn()
    leaves = []
    tree = {"children": [], "is_root": True, "history": [], "explored": False, "prob": 1.0, "stats": stats,
            "move_list": move_list, "color": rootcolor}

    def expand(node, stats, move_list):
        assert node["color"] in ['white', 'black']

        def child_prob_raw(i, move):
            # possible for book moves
            if "is_book" in move:
                return 1.0
            elif node["color"] == rootcolor:
                return move["visits"] ** 1.0
            else:
                return (move["policy_prob"] + move["visits"]) / 2.0

        def child_prob(i, move):
            return child_prob_raw(i, move) / probsum

        probsum = 0.0
        for (i, move) in enumerate(move_list):
            probsum += child_prob_raw(i, move)

        for (i, move) in enumerate(move_list):
            # Don't expand on the actual game line as a variation!
            if node["is_root"] and move["pos"] == game_move:
                node["children"].append(None)
                continue

            subhistory = node["history"][:]
            subhistory.append(move["pos"])
            prob = node["prob"] * child_prob(i, move)
            clr = "white" if node["color"] == "black" else "black"
            child = {"children": [], "is_root": False, "history": subhistory, "explored": False, "prob": prob,
                     "stats": {}, "move_list": [], "color": clr}
            node["children"].append(child)
            leaves.append(child)

        node["stats"] = stats
        node["move_list"] = move_list
        node["explored"] = True

        for i in range(len(leaves)):
            if leaves[i] is node:
                del leaves[i]
                break

    def search(node):
        for mv in node["history"]:
            leela.add_move(leela.whose_turn(), mv)
        stats, move_list, skipped = do_analyze(leela, base_dir, verbosity, args.variations_time)
        expand(node, stats, move_list)

        for mv in node["history"]:
            leela.pop_move()

    expand(tree, stats, move_list)
    for i in range(nodes_per_variation):
        if len(leaves) > 0:
            node = max(leaves, key=(lambda n: n["prob"]))
            search(node)

    def advance(cursor, color, mv):
        found_child_idx = None
        clr = 'W' if color == 'white' else 'B'

        for j in range(len(cursor.children)):
            if clr in cursor.children[j].keys() and cursor.children[j][clr].data[0] == mv:
                found_child_idx = j

        if found_child_idx is not None:
            cursor.next(found_child_idx)
        else:
            nnode = sgflib.Node()
            nnode.add_property(sgflib.Property(clr, [mv]))
            cursor.append_node(nnode)
            cursor.next(len(cursor.children) - 1)

    def record(node):
        if not node["is_root"]:
            annotations.annotate_sgf(cursor,
                                     annotations.format_winrate(node["stats"], node["move_list"], board_size, None),
                                     [], [])
            move_list_to_display = []

            # Only display info for the principal variation or for lines that have been explored.
            for i in range(len(node["children"])):
                child = node["children"][i]

                if child is not None and (i == 0 or child["explored"]):
                    move_list_to_display.append(node["move_list"][i])

            (analysis_comment, lb_values, tr_values) = annotations.format_analysis(node["stats"], move_list_to_display,
                                                                                   None)
            annotations.annotate_sgf(cursor, analysis_comment, lb_values, tr_values)

        for i in range(len(node["children"])):
            child = node["children"][i]

            if child is not None:
                if child["explored"]:
                    advance(cursor, node["color"], child["history"][-1])
                    record(child)
                    cursor.previous()
                # Only show variations for the principal line, to prevent info overload
                elif i == 0:
                    pv = node["move_list"][i]["pv"]
                    color = node["color"]
                    num_to_show = min(len(pv), max(1, len(pv) * 2 / 3 - 1))

                    if args.num_to_show is not None:
                        num_to_show = args.num_to_show

                    for k in range(int(num_to_show)):
                        advance(cursor, color, pv[k])
                        color = 'black' if color == 'white' else 'white'

                    for k in range(int(num_to_show)):
                        cursor.previous()

    record(tree)
