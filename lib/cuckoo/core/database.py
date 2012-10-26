# Copyright (C) 2010-2012 Cuckoo Sandbox Developers.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

import os
import sys
import json
from datetime import datetime

from lib.cuckoo.common.constants import CUCKOO_ROOT
from lib.cuckoo.common.exceptions import CuckooDatabaseError
from lib.cuckoo.common.exceptions import CuckooOperationalError
from lib.cuckoo.common.exceptions import CuckooDependencyError
from lib.cuckoo.common.config import Config
from lib.cuckoo.common.objects import File, URL
from lib.cuckoo.common.utils import create_folder

try:
    from sqlalchemy import create_engine, Column
    from sqlalchemy import Integer, String, Boolean, DateTime, Enum
    from sqlalchemy import ForeignKey, Text, Index
    from sqlalchemy.orm import sessionmaker, relationship
    from sqlalchemy.sql import func
    from sqlalchemy.ext.declarative import declarative_base
    from sqlalchemy.exc import SQLAlchemyError, IntegrityError
    from sqlalchemy.pool import NullPool
    Base = declarative_base()
except ImportError:
    raise CuckooDependencyError("SQLAlchemy library not found, "
                                "verify your setup")

class Task(Base):
    """Analysis task queue."""
    __tablename__ = "tasks"

    id = Column(Integer(), primary_key=True)
    target = Column(Text(), nullable=False)
    category = Column(String(255), nullable=False)
    timeout = Column(Integer(), server_default="0", nullable=False)
    priority = Column(Integer(), server_default="1", nullable=False)
    custom = Column(String(255), nullable=True)
    machine = Column(String(255), nullable=True)
    package = Column(String(255), nullable=True)
    options = Column(String(255), nullable=True)
    platform = Column(String(255), nullable=True)
    added_on = Column(DateTime(timezone=False),
                      default=datetime.now(),
                      nullable=False)
    completed_on = Column(DateTime(timezone=False), nullable=True)
    status = Column(Enum("pending",
                         "processing",
                         "failure",
                         "success",
                         name="status_type"),
                         server_default="pending",
                         nullable=False)
    sample_id = Column(Integer, ForeignKey("samples.id"), nullable=True)
    sample = relationship("Sample", backref="tasks")
    guest = relationship("Guest", uselist=False, backref="tasks")

    def to_dict(self):
        """Converts object to dict.
        @return: dict
        """
        d = {}
        for column in self.__table__.columns:
            value = getattr(self, column.name)
            if isinstance(value, datetime):
                d[column.name] = value.strftime("%Y-%m-%d %H:%M:%S")
            else:
                d[column.name] = value
        return d

    def to_json(self):
        """Converts object to JSON.
        @return: JSON data
        """
        return json.dumps(self.to_dict())

    def __init__(self, target=None):
        self.target = target

    def __repr__(self):
        return "<Task('%s','%s')>" % (self.id, self.target)

class Guest(Base):
    """Tracks guest run."""
    __tablename__ = "guests"

    id = Column(Integer(), primary_key=True)
    name = Column(String(255), nullable=False)
    label = Column(String(255), nullable=False)
    manager = Column(String(255), nullable=False)
    started_on = Column(DateTime(timezone=False),
                        default=datetime.now(),
                        nullable=False)
    shutdown_on = Column(DateTime(timezone=False), nullable=True)
    task_id = Column(Integer,
                     ForeignKey('tasks.id'),
                     nullable=False,
                     unique=True)

    def __repr__(self):
        return "<Guest('%s','%s')>" % (self.id, self.name)

    def __init__(self, name, label, manager):
        self.name = name
        self.label = label
        self.manager = manager

class Sample(Base):
    """Submitted files details."""
    __tablename__ = "samples"

    id = Column(Integer(), primary_key=True)
    file_size = Column(Integer(), nullable=False)
    file_type = Column(String(255), nullable=False)
    md5 = Column(String(32), nullable=False)
    crc32 = Column(String(8), nullable=False)
    sha1 = Column(String(40), nullable=False)
    sha256 = Column(String(64), nullable=False)
    sha512 = Column(String(128), nullable=False)
    ssdeep = Column(String(255), nullable=True)
    __table_args__ = (Index("hash_index",
                            "md5",
                            "crc32",
                            "sha1",
                            "sha256",
                            "sha512",
                            unique=True), )

    def __repr__(self):
        return "<Sample('%s','%s')>" % (self.id, self.md5)

    def __init__(self,
                 md5,
                 crc32,
                 sha1,
                 sha256,
                 sha512,
                 file_size,
                 file_type=None,
                 ssdeep=None):
        self.md5 = md5
        self.sha1 = sha1
        self.crc32 = crc32
        self.sha256 = sha256
        self.sha512 = sha512
        self.file_size = file_size
        if file_type:
            self.file_type = file_type
        if ssdeep:
            self.ssdeep = ssdeep

class Database:
    """Analysis queue database.

    This class handles the creation of the database user for internal queue
    management. It also provides some functions for interacting with it.
    """

    def __init__(self, dsn=None):
        """@param dsn: database connection string."""
        cfg = Config()

        if dsn:
            self.engine = create_engine(dsn, poolclass=NullPool)
        elif cfg.cuckoo.database:
            self.engine = create_engine(cfg.cuckoo.database, poolclass=NullPool)
        else:
            db_file = os.path.join(CUCKOO_ROOT, "db", "cuckoo.db")
            if not os.path.exists(db_file):
                db_dir = os.path.dirname(db_file)
                if not os.path.exists(db_dir):
                    try:
                        create_folder(folder=db_dir)
                    except CuckooOperationalError as e:
                        raise CuckooDatabaseError("Unable to create database "
                                                  "directory: %s" % e)
            self.engine = create_engine("sqlite:///%s" % db_file)

        # Disable SQL logging. Turn it on for debugging.
        self.engine.echo = False
        # Connection timeout.
        if cfg.cuckoo.database_timeout:
            self.engine.pool_timeout = cfg.cuckoo.database_timeout
        else:
            self.engine.pool_timeout = 60
        # Create schema.
        try:
            Base.metadata.create_all(self.engine)
        except SQLAlchemyError as e:
            raise CuckooDatabaseError("Unable to create or connect to "
                                      "database: %s" % e)

        # Get db session.
        self.Session = sessionmaker(bind=self.engine)

    def __del__(self):
        """Disconnects pool."""
        self.engine.dispose()

    def _set_status(self, task_id, status):
        """Set task status.
        @param task_id: task identifier
        @param status: status string
        @return: operation status
        """
        session = self.Session()
        session.query(Task).get(task_id).status = status
        try:
            session.commit()
        except:
            session.rollback()
            return False

        return True

    def fetch(self):
        """Fetch a task.
        @return: task dict or None.
        """
        session = self.Session()
        row = session.query(Task).filter(Task.status == "pending").order_by("priority desc, added_on").first()
        return row

    def process(self, task_id):
        """Set task status as processing.
        @param task_id: task identifier
        @return: operation status
        """
        return self._set_status(task_id, "processing")

    def fetch_and_process(self):
        """Fetches a task waiting to be processed and locks it for processing.
        @return: None or task
        """
        session = self.Session()
        try:
            row = session.query(Task).filter(Task.status == "pending").order_by("priority desc, added_on").first()
            if row:
               row.status = "processing"
            session.commit()
        except SQLAlchemyError:
            session.rollback()
            return None
        return row

    def complete(self, task_id, success=True):
        """Mark a task as completed.
        @param task_id: task id.
        @param success: completed with status.
        @return: operation status.
        """
        session = self.Session()
        task = session.query(Task).get(task_id)

        if success:
            task.status = "success"
        else:
            task.status = "failure"

        task.completed_on = datetime.now()

        try:
            session.commit()
        except:
            session.rollback()
            return False

        return True

    def guest_start(self, task_id, name, label, manager):
        """Logs guest start.
        @param task_id: task identifier
        @param name: vm name
        @param label: vm label
        @param manager: vm manager
        @return: guest row id
        """
        session = self.Session()
        guest = Guest(name, label, manager)
        guest.started_on = datetime.now()
        session.query(Task).get(task_id).guest = guest
        try:
            session.commit()
        except:
            session.rollback()
            return None

        return guest.id

    def guest_stop(self, guest_id):
        """Logs guest stop.
        @param guest_id: guest log entry id
        """
        session = self.Session()
        session.query(Guest).get(guest_id).shutdown_on = datetime.now()
        try:
            session.commit()
        except:
            session.rollback()

    # The following functions are mostly used by external utils.

    def add(self,
            obj,
            timeout=0,
            package=None,
            options=None,
            priority=1,
            custom=None,
            machine=None,
            platform=None):
        """Add a task to database.
        @param file_path: sample path.
        @param timeout: selected timeout.
        @param options: analysis options.
        @param priority: analysis priority.
        @param custom: custom options.
        @param machine: selected machine.
        @param platform: platform
        @return: cursor or None.
        """
        session = self.Session()

        if isinstance(obj, File):
            try:
                sample = Sample(md5=obj.get_md5(),
                                crc32=obj.get_crc32(),
                                sha1=obj.get_sha1(),
                                sha256=obj.get_sha256(),
                                sha512=obj.get_sha512(),
                                file_size=obj.get_size(),
                                file_type=obj.get_type(),
                                ssdeep=obj.get_ssdeep())
                session.add(sample)
                session.commit()
            except IntegrityError:
                session.rollback()
                sample = session.query(Sample).filter(Sample.md5 == obj.get_md5()).first()

            task = Task(obj.file_path)
            task.sample_id = sample.id
        elif isinstance(obj, URL):
            task = Task(obj.url)

        task.category = obj.__class__.__name__.lower()
        task.timeout = timeout
        task.package = package
        task.options = options
        task.priority = priority
        task.custom = custom
        task.machine = machine
        task.platform = platform
        session.add(task)

        try:
            session.commit()
        except:
            session.rollback()
            return None

        return task.id

    def add_path(self,
                 file_path,
                 timeout=0,
                 package=None,
                 options=None,
                 priority=1,
                 custom=None,
                 machine=None,
                 platform=None):
        """Add a task to database from file path.
        @param file_path: sample path.
        @param timeout: selected timeout.
        @param options: analysis options.
        @param priority: analysis priority.
        @param custom: custom options.
        @param machine: selected machine.
        @param platform: platform
        @return: cursor or None.
        """
        if not file_path or not os.path.exists(file_path):
            return None

        return self.add(File(file_path),
                        timeout,
                        package,
                        options,
                        priority,
                        custom,
                        machine,
                        platform)

    def add_url(self,
                url,
                timeout=0,
                package=None,
                options=None,
                priority=1,
                custom=None,
                machine=None,
                platform=None):
        """Add a task to database from url.
        @param url: url.
        @param timeout: selected timeout.
        @param options: analysis options.
        @param priority: analysis priority.
        @param custom: custom options.
        @param machine: selected machine.
        @param platform: platform
        @return: cursor or None.
        """
        return self.add(URL(url),
                        timeout,
                        package,
                        options,
                        priority,
                        custom,
                        machine,
                        platform)

    def list_tasks(self, limit=None):
        """Retrieve list of task.
        @param limit: specify a limit of entries.
        @return: list of tasks.
        """
        session = self.Session()
        tasks = session.query(Task).order_by("added_on desc").limit(limit)
        return tasks

    def view_task(self, task_id):
        """Retrieve information on a task.
        @param task_id: ID of the task to query.
        @return: details on the task.
        """
        session = self.Session()
        task = session.query(Task).get(task_id)
        return task

    def view_sample(self, sample_id):
        """Retrieve information on a sample.
        @param sample_id: ID of the sample to query.
        @return: details on the sample.
        """
        session = self.Session()
        sample = session.query(Sample).get(sample_id)
        return sample
