from base64 import b64decode, b64encode
from click import get_current_context
from cryptography.fernet import Fernet
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from flask_login import current_user
from importlib import import_module
from json import load
from logging.config import dictConfig
from logging import getLogger, info
from os import getenv
from passlib.hash import argon2
from redis import Redis
from redis.exceptions import ConnectionError, TimeoutError
from requests import Session as RequestSession
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from smtplib import SMTP
from sys import path as sys_path
from traceback import format_exc
from warnings import warn

try:
    from hvac import Client as VaultClient
except ImportError:
    warn(f"Couldn't import hvac module ({format_exc()})")

try:
    from ldap3 import Server
except ImportError as exc:
    warn(f"Couldn't import ldap3 module ({exc})")

try:
    from tacacs_plus.client import TACACSClient
except ImportError as exc:
    warn(f"Couldn't import tacacs_plus module ({exc})")

from eNMS.database import db
from eNMS.variables import vs


class Environment:
    def __init__(self):
        self.scheduler_address = getenv("SCHEDULER_ADDR")
        self.init_authentication()
        self.init_encryption()
        self.use_vault = vs.settings["vault"]["use_vault"]
        if vs.settings["paths"]["custom_code"]:
            sys_path.append(vs.settings["paths"]["custom_code"])
        self.init_logs()
        self.init_redis()
        self.init_connection_pools()

    def detect_cli(self):
        try:
            return get_current_context().info_name == "flask"
        except RuntimeError:
            return False

    def encrypt_password(self, password):
        if isinstance(password, str):
            password = str.encode(password)
        return self.encrypt(password)

    def get_password(self, password):
        if not password:
            return
        if self.fernet_encryption and isinstance(password, str):
            password = str.encode(password)
        return str(self.decrypt(password), "utf-8")

    def init_authentication(self):
        ldap_address, tacacs_address = getenv("LDAP_ADDR"), getenv("TACACS_ADDR")
        try:
            if ldap_address:
                self.ldap_server = Server(getenv("LDAP_ADDR"))
            if tacacs_address:
                self.tacacs_client = TACACSClient(
                    getenv("TACACS_ADDR"), 49, getenv("TACACS_PASSWORD")
                )
        except NameError as exc:
            warn(f"Module missing ({exc})")

    def init_connection_pools(self):
        self.request_session = RequestSession()
        retry = Retry(**vs.settings["requests"]["retries"])
        for protocol in ("http", "https"):
            self.request_session.mount(
                f"{protocol}://",
                HTTPAdapter(max_retries=retry, **vs.settings["requests"]["pool"]),
            )

    def init_encryption(self):
        self.fernet_encryption = getenv("FERNET_KEY")
        if self.fernet_encryption:
            fernet = Fernet(self.fernet_encryption)
            self.encrypt, self.decrypt = fernet.encrypt, fernet.decrypt
        else:
            self.encrypt, self.decrypt = b64encode, b64decode

    def init_logs(self):
        folder = vs.path / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        with open(vs.path / "setup" / "logging.json", "r") as logging_config:
            logging_config = load(logging_config)
        dictConfig(logging_config)
        for logger, log_level in logging_config["external_loggers"].items():
            info(f"Changing {logger} log level to '{log_level}'")
            log_level = getattr(import_module("logging"), log_level.upper())
            getLogger(logger).setLevel(log_level)

    def init_redis(self):
        host = getenv("REDIS_ADDR")
        self.redis_queue = (
            Redis(
                host=host,
                port=6379,
                db=0,
                charset="utf-8",
                decode_responses=True,
                socket_timeout=0.1,
            )
            if host
            else None
        )

    def log(self, severity, content, user=None, change_log=True, logger="root"):
        logger_settings = vs.logging["loggers"].get(logger, {})
        if logger:
            getattr(getLogger(logger), severity)(content)
        if change_log or logger and logger_settings.get("change_log"):
            db.factory(
                "changelog",
                **{
                    "severity": severity,
                    "content": content,
                    "user": user or getattr(current_user, "name", ""),
                },
            )
        return logger_settings

    def log_queue(self, runtime, service, log=None, mode="add", start_line=0):
        if self.redis_queue:
            key = f"{runtime}/{service}/logs"
            vs.run_logs[runtime][int(service)] = None
            if mode == "add":
                log = self.redis("lpush", key, log)
            else:
                log = self.redis("lrange", key, 0, -1)
                if log:
                    log = log[::-1][start_line:]
        else:
            if mode == "add":
                return vs.run_logs[runtime][int(service)].append(log)
            else:
                full_log = getattr(vs.run_logs[runtime], mode)(int(service), [])
                log = full_log[start_line:]
        return log

    def redis(self, operation, *args, **kwargs):
        try:
            return getattr(self.redis_queue, operation)(*args, **kwargs)
        except (ConnectionError, TimeoutError) as exc:
            self.log("error", f"Redis Queue Unreachable ({exc})", change_log=False)

    def send_email(
        self,
        subject,
        content,
        recipients="",
        reply_to=None,
        sender=None,
        filename=None,
        file_content=None,
    ):
        sender = sender or vs.settings["mail"]["sender"]
        message = MIMEMultipart()
        message["From"] = sender
        message["To"] = recipients
        message["Date"] = formatdate(localtime=True)
        message["Subject"] = subject
        message.add_header("reply-to", reply_to or vs.settings["mail"]["reply_to"])
        message.attach(MIMEText(content))
        if filename:
            attached_file = MIMEApplication(file_content, Name=filename)
            attached_file["Content-Disposition"] = f'attachment; filename="{filename}"'
            message.attach(attached_file)
        smtp_args = (vs.settings["mail"]["server"], vs.settings["mail"]["port"])
        with SMTP(*smtp_args) as server:
            if vs.settings["mail"]["use_tls"]:
                server.starttls()
                password = getenv("MAIL_PASSWORD", "")
                server.login(vs.settings["mail"]["username"], password)
            server.sendmail(sender, recipients.split(","), message.as_string())


env = Environment()
