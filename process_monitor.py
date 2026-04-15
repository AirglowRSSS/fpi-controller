#!/usr/bin/env python3
import logging
import logging.handlers
import os
import socket
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
import paramiko
import MySQLdb as mdb
from config import config, processes_to_monitor
import utilities.time_helper

site_id = config['site'].lower()

log_file = os.path.join(config['log_dir'], 'process_monitor.log')
handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=1 * 1024 * 1024, backupCount=3)
handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)-8s %(message)s'))
logging.basicConfig(level=logging.DEBUG, handlers=[handler])

# Suppress noisy paramiko output
logging.getLogger("paramiko").setLevel(logging.WARNING)


@contextmanager
def ssh_tunnel(ssh_host, ssh_port, ssh_user, ssh_key_path,
               remote_host, remote_port, local_port=0):
    """Native paramiko replacement for SSHTunnelForwarder."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=ssh_host,
        port=ssh_port,
        username=ssh_user,
        key_filename=os.path.expanduser(ssh_key_path),
    )

    transport = client.get_transport()

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(('127.0.0.1', local_port))
    server_sock.listen(1)
    actual_local_port = server_sock.getsockname()[1]

    stop_event = threading.Event()

    def forward():
        server_sock.settimeout(1.0)
        while not stop_event.is_set():
            try:
                client_sock, _ = server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                channel = transport.open_channel(
                    'direct-tcpip',
                    (remote_host, remote_port),
                    client_sock.getpeername(),
                )
            except Exception:
                client_sock.close()
                continue

            def pipe(src, dst):
                try:
                    while True:
                        data = src.recv(1024)
                        if not data:
                            break
                        dst.sendall(data)
                except Exception:
                    pass
                finally:
                    src.close()
                    dst.close()

            threading.Thread(target=pipe, args=(client_sock, channel), daemon=True).start()
            threading.Thread(target=pipe, args=(channel, client_sock), daemon=True).start()

    t = threading.Thread(target=forward, daemon=True)
    t.start()

    try:
        yield actual_local_port
    finally:
        stop_event.set()
        server_sock.close()
        client.close()
        t.join(timeout=5)


def update_database(process_cmd, status, site_id):
    logging.debug("Updating database: process='%s', site='%s', status=%d", process_cmd, site_id, status)
    with ssh_tunnel('airglowgroup.web.illinois.edu', 22, 'airglowgroup',
                    '/home/airglow/.ssh/id_rsa', '127.0.0.1', 3306) as local_port:
        try:
            con = mdb.connect(host='127.0.0.1', db='airglowgroup_sitestatus', port=local_port, read_default_file="/home/airglow/.my2.cnf")
            cursor = con.cursor()
            current_time = datetime.now(timezone.utc)
            sql = """
            INSERT INTO process_status (process_name, site_id, status, last_checked)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE status = VALUES(status), last_checked = VALUES(last_checked)
            """
            cursor.execute(sql, (process_cmd, site_id, status, current_time))
            con.commit()
            logging.debug("Database updated for '%s'", process_cmd)
        except mdb.Error as e:
            logging.error("Database error for '%s': %s", process_cmd, e)
        finally:
            if con:
                cursor.close()
                con.close()


def is_within_time_window(start, end):
    """Return True if the current local time falls within [start, end] (crosses midnight if end < start)."""
    now = datetime.now().time()
    start_time = datetime.strptime(start, "%H:%M").time()
    end_time = datetime.strptime(end, "%H:%M").time()

    if end_time < start_time:
        return now >= start_time or now <= end_time
    else:
        return start_time <= now <= end_time


def is_process_running(process_cmd):
    try:
        subprocess.check_output(["pgrep", "-f", process_cmd])
        return True
    except subprocess.CalledProcessError:
        return False


def start_process(command):
    try:
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         shell=True, preexec_fn=os.setpgrp)
        logging.info("Restart command issued for: %s", command)
    except Exception as e:
        logging.error("Failed to start process '%s': %s", command, e)


# Main script logic
for process_name, process_info in processes_to_monitor.items():
    start_time = process_info['start_time']
    stop_time = process_info['stop_time']
    full_process_cmd = process_info['command']

    in_window = start_time is None or is_within_time_window(start_time, stop_time)
    logging.debug("Checking '%s': in_time_window=%s", process_name, in_window)

    if not in_window:
        continue

    running = is_process_running(process_name)
    status = 1 if running else 0
    logging.debug("Process '%s': %s", process_name, 'running' if running else 'NOT running')
    update_database(process_name, status, site_id)

    if not running:
        if process_name == 'main_scheduler.py':
            timeHelper = utilities.time_helper.TimeHelper()
            sunrise = timeHelper.getSunrise()
            if datetime.now() > sunrise:
                logging.debug("'%s' not running but past sunrise; skipping restart", process_name)
                continue

        logging.info("Process '%s' is not running; attempting restart", process_name)
        start_process(full_process_cmd)
