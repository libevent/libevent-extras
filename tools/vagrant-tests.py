#!/usr/bin/env python3

import os, sys, argparse, logging
import fcntl
from termcolor import colored
import vagrant
import subprocess, threading, multiprocessing

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from glob import glob
import re

def unlink(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass

class Box:
    def __init__(self, name, **kwargs):
        self.name = name
        self.timeout = kwargs.pop('timeout', 0)
        self.no_pkg = kwargs.pop('no_pkg', False)
        self.no_cmake = kwargs.pop('no_cmake', False)
        self.no_autotools = kwargs.pop('no_autotools', False)
        self._env = os.environ.copy()
        self.last = False
        self.box = None

    def run(self):
        result = {}

        disabled = { 'status': True }

        prepared = disabled
        if not self.no_pkg:
            prepared = self._run("prepare", "NO_PKG")

        result["{}_prepare".format(self.name)] = prepared

        if prepared['status']:
            cmake = disabled
            if not self.no_cmake:
                cmake = self._run("cmake", "NO_CMAKE")
            result["{}_cmake".format(self.name)] = cmake

            autotools = disabled
            if not self.no_autotools:
                autotools = self._run("autotools", "NO_AUTOTOOLS")
            result["{}_autotools".format(self.name)] = autotools
        else:
            result["{}_cmake".format(self.name)] = disabled
            result["{}_autotools".format(self.name)] = disabled

        return result
    def _run(self, which, env):
        self.info(which)

        self.box = vagrant.Vagrant(
            err_cm=self._create_log(which, "stderr"),
            out_cm=self._create_log(which, "stdout"),
            env=self._box_env(env),
        )

        def target():
            self.debug("starting")
            self.last = self.provision()
            self.debug("finished: result={}".format(self.last))

        thread = threading.Thread(target=target)
        thread.start()
        thread.join(self.timeout)
        self.debug("joining")

        if thread.is_alive():
            self.warning("halting")
            self.halt()
            thread.join()

        self.box = None

        result = {}
        if self.last:
            self.info(colored("PASSED {}".format(which), "green"))
            result['status'] = True
        else:
            self.info(colored("FAILED {}".format(which), "red"))
            result['status'] = False
            result['failed_tests'] = self.parse_logs(which)
        return result

    def provision(self):
        try:
            self.box.up(
                vm_name=self.name,
                provision=False,
            )
            self.box.provision(vm_name=self.name)
        except subprocess.CalledProcessError:
            return False
        return True
    def halt(self):
        try:
            self.box.halt(vm_name=self.name)
        except subprocess.CalledProcessError:
            pass

    def parse_logs(self, which):
        self.debug("parsing logs")

        failed = []
        failures = re.compile(r"^.*\[(?P<name>[^ ]*) FAILED\]$")
        for line in self.stdout(which).split("\n"):
            match = failures.match(line)
            if not match:
                continue
            key = "{}_{}".format(which, match.group("name"))
            self.info(colored("FAILED {}".format(key), "red"))
            failed.append(key)
        return failed

    def stdout(self, which):
        return self._read(self._log(which, "stdout"))
    def stderr(self, which):
        return self._read(self._log(which, "stderr"))

    def warning(self, message):
        logging.warning("box[name={}] {}".format(self.name, message))
    def info(self, message):
        logging.info("box[name={}] {}".format(self.name, message))
    def debug(self, message):
        logging.debug("box[name={}] {}".format(self.name, message))

    def _box_env(self, key):
        env = self._env
        env['NO_PKG'] = "true"
        env['NO_CMAKE'] = "true"
        env['NO_AUTOTOOLS'] = "true"
        env[key] = "false"
        return env
    def _create_log(self, which, std):
        log = self._log(which, std)
        unlink(log)
        return vagrant.make_file_cm(log)
    def _log(self, which, std):
        return ".vagrant/{}_{}_{}.log".format(self.name, which, std)

    @staticmethod
    def _read(path):
        with open(path) as fp:
            return fp.read()

def send_email(failed_boxes, failed_tests):
    me = "a3at.mail@gmail.com"
    to = me

    logging.debug("Sending report to {}".format(to))

    msg = MIMEMultipart()
    with open(".vagrant/tests.log") as f:
        msg.attach(MIMEText(f.read(), _charset="UTF-8"))
    subject = "libevent. tests"
    if failed_boxes > 0:
        subject += ". failed ({} failed boxes, {} failed tests)".format(
            failed_boxes, failed_tests
        )
    else:
        subject += ". success"
    msg['Subject'] = subject
    msg['To'] = to
    for file in glob(".vagrant/*.log"):
        f = open(file, "rb")
        m = MIMEText(f.read(), _charset="UTF-8")
        f.close()
        m.add_header('Content-Disposition', 'attachment', filename=file)
        msg.attach(m)

    s = smtplib.SMTP("localhost")
    s.sendmail(me, [to], msg.as_string())
    s.quit()

def boxes_list():
    names = []
    for v in vagrant.Vagrant().status():
        logging.debug("box[name={}] from 'vagrant status'".format(v.name))
        names.append(v.name)
    return names

def filter_boxes(boxes, available_boxes):
    for b in boxes:
        if not b in available_boxes:
            raise ValueError(b)
    return boxes

def reset_boxes(available_boxes):
    for b in available_boxes:
        logging.info("box[name={}] halt".format(b))
        vagrant.Vagrant().halt(vm_name=b)

def box_runner(opts):
    args = opts["args"]
    name = opts["name"]
    q    = opts["queue"]

    boxes_args = {
        "timeout":      args.timeout,
        "no_pkg":       args.no_pkg,
        "no_cmake":     args.no_cmake,
        "no_autotools": args.no_autotools,
    }

    q.put(Box(name, **boxes_args).run())

def run_boxes(available_boxes, args):
    manager = multiprocessing.Manager()
    queue = manager.Queue()

    boxes_configurations = []
    for b in available_boxes:
        boxes_configurations.append({
            "args":  args,
            "name":  b,
            "queue": queue
        })

    pool = multiprocessing.Pool(processes=args.workers)
    pool.map(box_runner, boxes_configurations)

    boxes_results = {}
    while not queue.empty():
        boxes_results = { **boxes_results, **queue.get(), }

    failed_boxes = 0
    unique_failed_tests = []
    for box, result in boxes_results.items():
        if result['status']:
            continue
        failed_boxes += 1

        for test in result['failed_tests']:
            unique_failed_tests.append('_'.join(test.split('_')[1:]))

    if failed_boxes:
        logging.info(colored("Failed boxes: {}".format(failed_boxes), "red"))

    failed_tests = 0
    if unique_failed_tests:
        unique_failed_tests = list(set(unique_failed_tests))
        failed_tests = len(unique_failed_tests)
        logging.info(colored("Failed tests: {}".format(failed_tests), "red"))
        logging.info(colored("Unique failed tests:", "red"))
        for test in unique_failed_tests:
            logging.info(colored("\t{}".format(test), "red"))

    if not args.no_email:
        send_email(failed_boxes, failed_tests)

    return 0 if failed_boxes == 0 else 1

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("-v", "--verbose", action="count")
    p.add_argument("-b", "--boxes", nargs="+",
                   help="By default it will run on every box that 'vagrant status' reports")
    p.add_argument("-t", "--timeout", type=int, default=60*60*1,
                   help="Try to avoid hanging, by issuing halt after this period, by default: %(default)s seconds")
    p.add_argument("--no-pkg", action="store_true",
                   help="Do not pre-install packages")
    p.add_argument("--no-cmake", action="store_true",
                   help="Do not compile with cmake")
    p.add_argument("--no-autotools", action="store_true",
                   help="Do not compile with autotools")
    p.add_argument("--no-email", action="store_true",
                   help="By default it will end email with report and attach logs")
    p.add_argument("--no-lock", action="store_true",
                   help="By default it will protect with .vagrant/lock file")
    p.add_argument("--logging-format",
                   default="%(asctime)s: %(levelname)s: %(module)s: %(message)s")
    p.add_argument("--root", default=os.getcwd())
    p.add_argument("--reset", action="store_true",
                   help="""
                   this will halt all boxes before running, since
                   vagrant synced_folders via rsync syncs only at start,
                   and if you change sources you should, use --reset
                   """)
    p.add_argument("--workers", type=int, default=1,
                   help="Run boxes configurations in parallel (default: %(default)s)")
    return p.parse_args()

def configure_logging(verbose, fmt):
    logging.basicConfig(
        format=fmt,
        level=logging.DEBUG if verbose else logging.INFO,
    )

    unlink(".vagrant/tests.log")

    fh = logging.FileHandler(".vagrant/tests.log")
    fh.setFormatter(logging.Formatter(fmt))
    logging.getLogger().addHandler(fh)

def main():
    args = parse_args()
    os.chdir(args.root)

    lock = None
    if not args.no_lock:
        lock = open(".vagrant/lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    configure_logging(args.verbose, args.logging_format)
    logging.debug("Args: {}".format(sys.argv))
    logging.info("root={}".format(args.root))

    try:
        available_boxes = boxes_list()

        if args.boxes:
            boxes = filter_boxes(args.boxes, available_boxes)
        else:
            boxes = available_boxes

        logging.debug("Enabled boxes: {}".format(boxes))

        if args.reset:
            reset_boxes(boxes)

        ret = run_boxes(boxes, args)
    except:
        raise
    finally:
        if lock != None:
            fcntl.flock(lock, fcntl.LOCK_UN)

    return ret

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
