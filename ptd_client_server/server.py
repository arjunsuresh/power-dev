#!/usr/bin/env python3
# Copyright 2018 The MLPerf Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =============================================================================

from __future__ import annotations
from typing import Optional, Dict, Tuple
from decimal import Decimal
from ipaddress import ip_address
import argparse
import base64
import configparser
import datetime
import logging
import os
import re
import socket
import subprocess
import sys
import time
import zipfile

import lib


RE_PTD_LOG = re.compile(
    r"""^
        Time,  [^,]*,
        Watts, [^,]*,
        Volts, (?P<v> [^,]* ),
        Amps,  (?P<a> [^,]* ),
        .*,
        Mark,  (?P<mark> [^,]* )
    $""",
    re.X,
)


def max_volts_amps(log_fname: str, mark: str) -> Tuple[str, str]:
    maxVolts = Decimal("-1")
    maxAmps = Decimal("-1")
    with open(log_fname, "r") as f:
        for line in f:
            m = RE_PTD_LOG.match(line.rstrip("\r\n"))
            if m and m["mark"] == mark:
                maxVolts = max(maxVolts, Decimal(m["v"]))
                maxAmps = max(maxAmps, Decimal(m["a"]))
    if maxVolts <= 0 or maxAmps <= 0:
        raise RuntimeError(f"Could not find values for {mark!r}")
    return str(maxVolts), str(maxAmps)


def read_log(log_fname: str, mark: str) -> str:
    result = []
    with open(log_fname, "r") as f:
        for line in f:
            m = RE_PTD_LOG.match(line.rstrip("\r\n"))
            if m and m["mark"] == mark:
                result.append(line)
    return "".join(result)


def exit_with_error_msg(error_msg: str) -> None:
    logging.fatal(error_msg)
    exit(1)


def get_host_port_from_listen_string(listen_str: str) -> Tuple[str, int]:
    try:
        host, port = listen_str.split(" ")
    except ValueError:
        raise ValueError(f"could not parse listen option {listen_str}")
    try:
        ip_address(host)
    except ValueError:
        raise ValueError(f"wrong listen option ip address {ip_address}")
    try:
        int_port = int(port)
    except ValueError:
        raise ValueError(f"could not parse listen option port {port} as integer")
    return (host, int_port)


class ServerConfig:
    def __init__(self, filename: str) -> None:
        conf = configparser.ConfigParser()
        conf.read_file(open(filename))

        try:
            serv_conf = conf["server"]
        except KeyError:
            exit_with_error_msg(
                "Server section is empty in the configuration file. "
                "Please add server section."
            )

        all_options = {
            "ntpServer",
            "outDir",
            "ptdCommand",
            "ptdLogfile",
            "ptdPort",
            "listen",
        }

        self.ntp_server = serv_conf.get("ntpServer")

        try:
            ptd_port = serv_conf["ptdPort"]
            self.ptd_logfile = serv_conf["ptdLogfile"]
            self.out_dir = serv_conf["outDir"]
            self.ptd_command = serv_conf["ptdCommand"]
        except KeyError as e:
            exit_with_error_msg(f"{filename}: missing option: {e.args[0]!r}")

        try:
            listen_str = serv_conf["listen"]
            try:
                self.host, self.port = get_host_port_from_listen_string(listen_str)
            except ValueError as e:
                exit_with_error_msg(f"{filename}: {e.args[0]}")
        except KeyError:
            self.host, self.port = (lib.DEFAULT_IP_ADDR, lib.DEFAULT_PORT)
            logging.warning(
                f"{filename}: There is no listen option. Server use {self.host}:{self.port}"
            )

        try:
            self.ptd_port = int(ptd_port)
        except ValueError:
            exit_with_error_msg(f"{filename}: could not parse {ptd_port!r} as int")

        unused_options = set(serv_conf.keys()) - set((i.lower() for i in all_options))
        if len(unused_options) != 0:
            logging.warning(
                f"{filename}: ignoring unknown options: {', '.join(unused_options)}"
            )

        unused_sections = set(conf.sections()) - {"server"}
        if len(unused_sections) != 0:
            logging.warning(
                f"{filename}: ignoring unknown sections: {', '.join(unused_sections)}"
            )


class Ptd:
    def __init__(self, command: str, port: int) -> None:
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._socket: Optional[socket.socket] = None
        self._proto: Optional[lib.Proto] = None
        self._command = command
        self._port = port
        self._init_Amps: Optional[str] = None
        self._init_Volts: Optional[str] = None

    def start(self) -> bool:
        if self._process is not None:
            return False
        if sys.platform == "win32":
            # shell=False:
            #   On Windows, we don't need a shell to run a command from a single
            #   string.  On the other hand, calling self._process.terminate()
            #   will terminate the shell (cmd.exe), but not the an actual
            #   command.  Thus, shell=False.
            #
            # creationflags=subprocess.CREATE_NEW_PROCESS_GROUP:
            #   We do not want to pass ^C from the current console to the
            #   PTDaemon.  Instead, we terminate it explicitly in self.stop().
            self._process = subprocess.Popen(
                self._command,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            self._process = subprocess.Popen(self._command, shell=True)

        retries = 100
        s = None
        while s is None and retries > 0:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.connect(("127.0.0.1", self._port))
            except ConnectionRefusedError:
                if lib.sig.stopped:
                    exit()
                time.sleep(0.1)
                s = None
                retries -= 1
        if s is None:
            logging.error("Could not connect to PTD")
            self.stop()
            return False
        self._socket = s
        self._proto = lib.Proto(s)

        if self.cmd("Hello") != "Hello, PTDaemon here!":
            logging.error("This is not PTDaemon")
            return False

        self.cmd("Identify")  # reply traced in logs

        logging.info("Connected to PTDaemon")

        self._get_initial_range()
        return True

    def stop(self) -> None:
        if self._proto is not None:
            self.cmd("Stop")
            self.cmd(f"SR,V,{self._init_Volts}")
            self.cmd(f"SR,A,{self._init_Amps}")
            logging.info(
                f"Set initial values for Amps {self._init_Amps} and Volts {self._init_Volts}"
            )
            self._proto = None

        if self._socket is not None:
            self._socket.close()
            self._socket = None

        if self._process is not None:
            logging.info("Stopping ptd...")
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
            self._process = None

    def running(self) -> bool:
        return self._process is not None

    def cmd(self, cmd: str) -> Optional[str]:
        if self._proto is None:
            return None
        logging.info(f"Sending to ptd: {cmd!r}")
        self._proto.send(cmd)
        reply = self._proto.recv()
        logging.info(f"Reply from ptd: {reply!r}")
        return reply

    def _get_initial_range(self) -> None:
        # Normal Response: ?Ranges,{Amp Autorange},{Amp Range},{Volt Autorange},{Volt Range}\r\n?
        # Values: for autorange settings, -1 indicates ?unknown?, 0 = disabled, 1 = enabled
        # For range values, -1.0 indicates ?unknown?, >0 indicates actual value
        response = self.cmd("RR")
        if response is None or response == "":
            logging.error("Can not get initial range")
            exit(1)

        response_list = response.split(",")

        def get_range_from_ranges_list(param_num: int, setting_name: str) -> str:
            try:
                if (
                    response_list[param_num] == "0"
                    and float(response_list[param_num + 1]) > 0
                ):
                    return response_list[param_num + 1]
            except ValueError:
                logging.warning(f"Can not get ptd range value for {setting_name}")
                return "Auto"
            return "Auto"

        self._init_Amps = get_range_from_ranges_list(1, "Amps")
        self._init_Volts = get_range_from_ranges_list(3, "Volts")
        logging.info(
            f"Initial range for Amps is {self._init_Amps} for Volts is {self._init_Volts}"
        )


class Server:
    def __init__(self, config: ServerConfig) -> None:
        self._ptd = Ptd(config.ptd_command, config.ptd_port)
        self._mode: Optional[str] = None
        self._mark: Optional[str] = None
        self._ranging_table: Dict[str, Tuple[str, str]] = {}
        self._config = config

    def close(self) -> None:
        self._ptd.stop()

    def handle_connection(self, p: lib.Proto) -> None:
        self._ranging_table = {}
        self._mode = None
        self._mark = None

        if os.path.exists(self._config.ptd_logfile):
            os.remove(self._config.ptd_logfile)

        try:
            while True:
                with lib.sig:
                    cmd = p.recv()

                if cmd is None:
                    logging.info("Connection closed")
                    break
                logging.info(f"Got command from the client {cmd!r}")

                try:
                    reply = self._handle_cmd(cmd, p)
                except KeyboardInterrupt:
                    break
                except Exception:
                    logging.exception("Got an exception")
                    reply = "Error: exception"

                if len(reply) < 1000:
                    logging.info(f"Sending reply to client {reply!r}")
                else:
                    logging.info(
                        f"Sending reply to client {reply[:50]!r}... len={len(reply)}"
                    )
                p.send(reply)
        finally:
            self._ptd.stop()

    def _set_range(self, volts_value: str, amps_value: str) -> None:
        self._ptd.cmd(f"SR,V,{volts_value}")
        self._ptd.cmd(f"SR,A,{amps_value}")
        logging.info("Wait 10 seconds to apply setting for ampere and voltage")
        with lib.sig:
            time.sleep(10)

    def _handle_cmd(self, cmd: str, p: lib.Proto) -> str:
        cmd = cmd.split(",")
        if len(cmd) == 0:
            return "..."
        if cmd[0] == "hello":
            return "Hello from server!"
        if cmd[0] == "time":
            return str(time.time())
        if cmd[0] == "init":
            lib.ntp_sync(config.ntp_server)
            if not self._ptd.start():
                return "Error"
            return "OK"
        if cmd[0] == "start-ranging" and len(cmd) == 2:
            self._set_range("Auto", "Auto")
            logging.info("Starting ranging mode")
            self._ptd.cmd(f"Go,1000,0,ranging-{cmd[1]}")
            self._mode = "ranging"
            self._mark = cmd[1]
            return "OK"
        if cmd[0] == "start-testing" and len(cmd) == 2:
            maxVolts, maxAmps = self._ranging_table[cmd[1]]
            self._set_range(maxVolts, maxAmps)
            logging.info("Starting testing mode")
            self._ptd.cmd(f"Go,1000,0,testing-{cmd[1]}")
            self._mode = "testing"
            self._mark = cmd[1]
            return "OK"
        if cmd[0] == "stop":
            if self._mark is None:
                return "Error"
            self._ptd.cmd("Stop")
            if self._mode == "ranging":
                item = max_volts_amps(self._config.ptd_logfile, "ranging-" + self._mark)
                logging.info(f"Result for {self._mark}: {item}")
                self._ranging_table[self._mark] = item
            self._last_log = read_log(
                self._config.ptd_logfile, f"{self._mode}-{self._mark}"
            )
            self._mode = None
            self._mark = None
            return "OK"
        if cmd[0] == "get-last-log":
            return "base64 " + base64.b64encode(self._last_log.encode()).decode()
        if cmd[0] == "get-log":
            with open(self._config.ptd_logfile, "rb") as f:
                data = f.read()
            return "base64 " + base64.b64encode(data).decode()
        if cmd[0] == "push-log" and len(cmd) == 2:
            label = cmd[1]
            if not lib.check_label(label):
                return "Error: invalid label"
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            dirname = os.path.join(self._config.out_dir, timestamp + "_" + label)
            try:
                p.recv_file(dirname + ".zip")
                with zipfile.ZipFile(dirname + ".zip", "r") as zf:
                    zf.extractall(dirname)
            finally:
                try:
                    os.remove(dirname + ".zip")
                except OSError:
                    pass
            return "OK"

        return "Error: unknown command"


lib.init("ptd-server")

parser = argparse.ArgumentParser(description="Server for communication with PTD")

# fmt: off
parser.add_argument("-c", "--configurationFile", metavar="FILE", type=str, help="", default="server.conf")
# fmt: on
args = parser.parse_args()

config = ServerConfig(args.configurationFile)

if not os.path.exists(config.out_dir):
    try:
        os.mkdir(config.out_dir)
    except FileNotFoundError:
        exit_with_error_msg(
            f"Could not create directory {config.out_dir!r}. "
            "Make sure all intermediate directories exist."
        )

lib.ntp_sync(config.ntp_server)

server = Server(config)
try:
    lib.run_server(config.host, config.port, server.handle_connection)
except KeyboardInterrupt:
    pass
finally:
    server.close()
