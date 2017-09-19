import time
import hashlib
import re
import sys
from queue import Queue, Empty
from threading import Thread
from subprocess import Popen, PIPE
import config

SGF_COORD = 'abcdefghijklmnopqrstuvwxy'
BOARD_COORD = 'abcdefghjklmnopqrstuvwxyz'  # without "i"

# Regex
update_regex = r""
finished_regex = r""
stats_regex = r""
bookmove_regex = r""
status_regex = r""
move_regex = r""
best_regex = r""


class ReaderThread:
    """
    ReaderThread perpetually reads from the given file descriptor and pushes the result to a queue.
    """

    def __init__(self, fd):
        self.queue = Queue()
        self.fd = fd  # stdout or stderr is given
        self.stopped = False

    def stop(self):
        """
        No lock since this is just a simple bool that only ever changes one way
        """
        self.stopped = True

    def loop(self):
        """
        Loop fd.readline() due to EOF until the process is closed
        """
        while not self.stopped and not self.fd.closed:
            try:
                line = self.fd.readline()
                if len(line) > 0:
                    self.queue.put(line)
            except IOError:
                time.sleep(0.1)
                pass

    def readline(self):
        """
        Read single line from queue
        :return: str
        """
        try:
            line = self.queue.get_nowait()
            return line
        except Empty:
            return ""

    def read_all_lines(self):
        """
        Read all lines from queue.
        :return: list
        """
        lines = []

        while True:
            try:
                line = self.queue.get_nowait()
                lines.append(line)
            except Empty:
                break

        return lines


#
def start_reader_thread(fd):
    """
    Start file descriptor loop thread
    :param fd: stdout | stderr
    :return: ReaderThread
    """
    rt = ReaderThread(fd)
    t = Thread(target=rt.loop())
    t.start()

    return rt


class CLI(object):
    """
    Command Line Interface object designed to work with Ray.
    """

    def __init__(self, board_size, executable, is_handicap_game, komi, seconds_per_search, verbosity):
        self.board_size = board_size
        self.executable = executable
        self.is_handicap_game = is_handicap_game
        self.komi = komi
        self.seconds_per_search = seconds_per_search
        self.verbosity = verbosity

        self.p = None
        self.stdout_thread = None
        self.stderr_thread = None
        self.history = []

    def convert_position(self, pos):
        """
        Convert SGF coordinates to board position coordinates
        Example aa -> a1, qq -> p15
        :param pos: string
        :return: string
        """

        x = BOARD_COORD[SGF_COORD.index(pos[0])]
        y = self.board_size - SGF_COORD.index(pos[1])

        return '%s%d' % (x, y)

    def parse_position(self, pos):
        """
        Convert board position coordinates to SGF coordinates
        Example A1 -> aa, P15 -> qq
        :param pos: string
        :return: string
        """

        # Pass moves are the empty string in sgf files
        if pos == "pass":
            return ""

        x = BOARD_COORD.index(pos[0].lower())
        y = self.board_size - int(pos[1:])

        return "%s%s" % (SGF_COORD[x], SGF_COORD[y])

    def history_hash(self):
        """
        Return hash for checkpoint filename
        :return: string
        """
        h = hashlib.md5()

        for cmd in self.history:
            _, c, p = cmd.split()
            h.update(bytes((c[0] + p), 'utf-8'))

        return h.hexdigest()

    def add_move(self, color, pos):
        """
        Convert given SGF coordinates to board coordinates and writes them to history as a command
        :param color: str
        :param pos: str
        """
        move = 'pass' if pos in ['', 'tt'] else self.convert_position(pos)
        cmd = "play %s %s" % (color, move)
        self.history.append(cmd)

    def pop_move(self):
        self.history.pop()

    def clear_history(self):
        self.history.clear()

    def whose_turn(self):
        if len(self.history) == 0:
            return "white" if self.is_handicap_game else "black"
        else:
            return "black" if "white" in self.history[-1] else "white"

    @staticmethod
    def to_fraction(v):
        return 0.01 * float(v.strip())

    def parse_status_update(self, message):
        m = re.match(update_regex, message)

        if m is not None:
            visits = int(m.group(1))
            winrate = self.to_fraction(m.group(2))
            seq = m.group(3)
            seq = [self.parse_position(p) for p in seq.split()]

            return {'visits': visits, 'winrate': winrate, 'seq': seq}
        return {}
        pass

    def drain(self):
        """
        Drain all remaining stdout and stderr contents
        """
        so = self.stdout_thread.read_all_lines()
        se = self.stderr_thread.read_all_lines()
        return so, se

    def send_command(self, cmd, expected_success_count=1, drain=True):
        """
        Send command to Ray and drains stdout/stderr
        :param cmd: string
        :param expected_success_count: how many '=' should Ray return
        :param drain: should drain or not
        """
        tries = 0
        success_count = 0
        timeout = 200

        # Sending command
        self.p.stdin.write(cmd + "\n")
        self.p.stdin.flush()

        while tries <= timeout and self.p is not None:
            # Loop readline until reach given number of success
            while True:
                s = self.stdout_thread.readline()

                # Ray follows GTP and prints a line starting with "=" upon success.
                if '=' in s:
                    success_count += 1
                    if success_count >= expected_success_count:
                        if drain:
                            self.drain()
                        return None

                # Break readline loop, sleep and wait for more
                if s == "":
                    break

            time.sleep(0.1)
            tries += 1

        raise Exception("Failed to send command '%s' to Ray" % cmd)

    def start(self):
        """
        Start Ray process
        """
        if self.verbosity > 0:
            print("Starting ray...", file=sys.stderr)

        p = Popen(self.executable + config.ray_settings, stdout=PIPE, stdin=PIPE, stderr=PIPE,
                  universal_newlines=True)

        self.p = p
        self.stdout_thread = start_reader_thread(p.stdout)
        self.stderr_thread = start_reader_thread(p.stderr)

        time.sleep(2)

        if self.verbosity > 0:
            print("Setting board size %d and komi %f to Leela" % (self.board_size, self.komi), file=sys.stderr)

        self.send_command('boardsize %d' % self.board_size)
        self.send_command('komi %f' % self.komi)
        self.send_command('time_settings 0 %d 1' % self.seconds_per_search)

    def stop(self):
        """
        Stop Ray process
        """
        if self.verbosity > 0:
            print("Stopping ray...", file=sys.stderr)

        if self.p is not None:
            p = self.p
            stdout_thread = self.stdout_thread
            stderr_thread = self.stderr_thread
            self.p = None
            self.stdout_thread = None
            self.stderr_thread = None
            stdout_thread.stop()
            stderr_thread.stop()

            try:
                p.stdin.write('quit\n')
                p.stdin.flush()
            except IOError:
                pass

            time.sleep(0.1)
            try:
                p.terminate()
            except OSError:
                pass

    def play_move(self, pos):
        """
        Play move
        :param pos: string
        """
        color = self.whose_turn()
        cmd = 'play %s %s' % (color, pos)
        self.send_command(cmd)

    def reset(self):
        """
        Clear board
        """
        self.send_command('clear_board')

    def board_state(self):
        """
        Show board
        """
        self.send_command("showboard", drain=False)
        (so, se) = self.drain()
        return "".join(se)

    def go_to_position(self):
        """
        Send all moves from history to Ray
        """
        count = len(self.history)
        cmd = "\n".join(self.history)
        self.send_command(cmd, expected_success_count=count)

    def parse(self, stdout, stderr):
        if self.verbosity > 2:
            print("RAY STDOUT:\n" + "".join(stdout) + "\nEND OF RAY STDOUT", file=sys.stderr)
            print("RAY STDERR:\n" + "".join(stderr) + "\nEND OF RAY STDERR", file=sys.stderr)

        stats = {}
        move_list = []

        def flip_winrate(wr):
            return (1.0 - wr) if self.whose_turn() == "white" else wr

        finished = False
        summarized = False

        for line in stderr:
            line = line.strip()
            if line.startswith('================'):
                finished = True

            # Find bookmove string
            m = re.match(bookmove_regex, line)

            if m is not None:
                stats['bookmoves'] = int(m.group(1))
                stats['positions'] = int(m.group(2))

            # Find status string
            m = re.match(status_regex, line)

            if m is not None:
                stats['mc_winrate'] = flip_winrate(float(m.group(1)))
                stats['nn_winrate'] = flip_winrate(float(m.group(2)))
                stats['margin'] = m.group(3)

            # Find move string
            m = re.match(move_regex, line)
            if m is not None:
                pos = self.parse_position(m.group(1))
                visits = int(m.group(2))
                winrate = flip_winrate(self.to_fraction(m.group(3)))
                mc_winrate = flip_winrate(self.to_fraction(m.group(4)))
                nn_winrate = flip_winrate(self.to_fraction(m.group(5)))
                nn_count = int(m.group(6))
                policy_prob = self.to_fraction(m.group(7))
                pv = [self.parse_position(p) for p in m.group(8).split()]

                info = {
                    'pos': pos,
                    'visits': visits,
                    'winrate': winrate,
                    'mc_winrate': mc_winrate,
                    'nn_winrate': nn_winrate,
                    'nn_count': nn_count,
                    'policy_prob': policy_prob,
                    'pv': pv
                }
                move_list.append(info)

            if finished and not summarized:
                m = re.match(best_regex, line)

                # Parse best move and its winrate
                if m is not None:
                    stats['best'] = self.parse_position(m.group(3).split()[0])
                    stats['winrate'] = flip_winrate(self.to_fraction(m.group(2)))

                m = re.match(stats_regex, line)

                # Parse number of visits to stats
                if m is not None:
                    stats['visits'] = int(m.group(1))
                    summarized = True

        # Find finished string
        m = re.search(finished_regex, "".join(stdout))

        # Parse chosen move to stats
        if m is not None:
            stats['chosen'] = "resign" if m.group(1) == "resign" else self.parse_position(m.group(1))

        # Parse bookmoves
        if 'bookmoves' in stats and len(move_list) == 0:
            move_list.append({'pos': stats['chosen'], 'is_book': True})
        else:
            required_keys = ['mc_winrate', 'margin', 'best', 'winrate', 'visits']

            # Check for missed data
            for k in required_keys:
                if k not in stats:
                    print("WARNING: analysis stats missing %s data" % k, file=sys.stderr)

            move_list = sorted(move_list,
                               key=(lambda key: 1000000000000000 if info['pos'] == stats['best'] else info['visits']),
                               reverse=True)
            move_list = [info for (i, info) in enumerate(move_list) if i == 0 or info['visits'] > 0]

            # In the case where ray resigns, just replace with the move Leela did think was best
            if stats['chosen'] == "resign":
                stats['chosen'] = stats['best']

        return stats, move_list

    def analyze(self):
        """
        Analyze current position with given time settings
        :return: tuple
        """
        p = self.p
        if self.verbosity > 1:
            print("Analyzing state: " + self.whose_turn() + " to play", file=sys.stderr)
            print(self.board_state(), file=sys.stderr)

        # Set time for move
        self.send_command('time_left black %d 1' % self.seconds_per_search)
        self.send_command('time_left white %d 1' % self.seconds_per_search)

        # Generate next move
        cmd = "genmove %s\n" % self.whose_turn()
        p.stdin.write(cmd)
        p.stdin.flush()

        updated = 0
        stderr = []
        stdout = []

        # Some scary loop to find end of analysis
        while updated < 20 + self.seconds_per_search * 2 and self.p is not None:
            out, err = self.drain()
            stdout.extend(out)
            stderr.extend(err)
            d = self.parse_status_update("".join(err))

            if 'visits' in d:
                if self.verbosity > 0:
                    print("Visited %d positions" % d['visits'], file=sys.stderr)
                updated = 0

            updated += 1

            if re.search(finished_regex, ''.join(stdout)) is not None:
                if re.search(stats_regex, ''.join(stderr)) is not None \
                        or re.search(bookmove_regex, ''.join(stderr)) is not None:
                    break

            time.sleep(1)

        #
        p.stdin.write("\n")
        p.stdin.flush()
        time.sleep(1)

        out, err = self.drain()
        stdout.extend(out)
        stderr.extend(err)

        stats, move_list = self.parse(stdout, stderr)

        if self.verbosity > 0:
            print("Chosen move: %s" % stats['chosen'], file=sys.stderr)

            if 'best' in stats:
                print("Best move: %s" % stats['best'], file=sys.stderr)
                print("Winrate: %f" % stats['winrate'], file=sys.stderr)
                print("Visits: %d" % stats['visits'], file=sys.stderr)

        return stats, move_list
