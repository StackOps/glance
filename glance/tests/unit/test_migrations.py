# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010-2011 OpenStack, LLC
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Tests for database migrations. This test case reads the configuration
file /tests/unit/test_migrations.conf for database connection settings
to use in the tests. For each connection found in the config file,
the test case runs a series of test cases to ensure that migrations work
properly both upgrading and downgrading, and that no data loss occurs
if possible.
"""

import ConfigParser
import datetime
import os
import urlparse

from migrate.versioning.repository import Repository
from oslo.config import cfg
from sqlalchemy import *
from sqlalchemy.pool import NullPool

from glance.common import crypt
from glance.common import exception
#NOTE(bcwaldon): import this to prevent circular import
from glance.db.sqlalchemy import api
import glance.db.sqlalchemy.migration as migration_api
from glance.db.sqlalchemy import models
from glance.tests import utils


CONF = cfg.CONF
CONF.import_opt('metadata_encryption_key', 'glance.registry')


class TestMigrations(utils.BaseTestCase):

    """Test sqlalchemy-migrate migrations"""

    TEST_DATABASES = {}
    # Test machines can set the GLANCE_TEST_MIGRATIONS_CONF variable
    # to override the location of the config file for migration testing
    CONFIG_FILE_PATH = os.environ.get('GLANCE_TEST_MIGRATIONS_CONF',
                                      os.path.join('glance', 'tests', 'unit',
                                                   'test_migrations.conf'))
    REPOSITORY_PATH = 'glance/db/sqlalchemy/migrate_repo'
    REPOSITORY = Repository(REPOSITORY_PATH)

    def __init__(self, *args, **kwargs):
        super(TestMigrations, self).__init__(*args, **kwargs)

    def setUp(self):
        super(TestMigrations, self).setUp()
        # Load test databases from the config file. Only do this
        # once. No need to re-run this on each test...
        if not TestMigrations.TEST_DATABASES:
            if os.path.exists(TestMigrations.CONFIG_FILE_PATH):
                cp = ConfigParser.RawConfigParser()
                try:
                    cp.read(TestMigrations.CONFIG_FILE_PATH)
                    defaults = cp.defaults()
                    for key, value in defaults.items():
                        TestMigrations.TEST_DATABASES[key] = value
                except ConfigParser.ParsingError, e:
                    self.fail("Failed to read test_migrations.conf config "
                              "file. Got error: %s" % e)
            else:
                self.fail("Failed to find test_migrations.conf config "
                          "file.")
        self.engines = {}
        for key, value in TestMigrations.TEST_DATABASES.items():
            self.engines[key] = create_engine(value, poolclass=NullPool)

        # We start each test case with a completely blank slate.
        self._reset_databases()

    def tearDown(self):
        super(TestMigrations, self).tearDown()
        # We destroy the test data store between each test case,
        # and recreate it, which ensures that we have no side-effects
        # from the tests
        self._reset_databases()

    def _reset_databases(self):
        def _is_sqlite(conn_string):
            return conn_string.startswith('sqlite')

        def _is_mysql(conn_string):
            return conn_string.startswith('mysql')

        def _is_postgresql(conn_string):
            return conn_string.startswith('postgresql')

        for key, engine in self.engines.items():
            conn_string = TestMigrations.TEST_DATABASES[key]
            conn_pieces = urlparse.urlparse(conn_string)
            if _is_sqlite(conn_string):
                # We can just delete the SQLite database, which is
                # the easiest and cleanest solution
                db_path = conn_pieces.path.strip('/')
                if os.path.exists(db_path):
                    os.unlink(db_path)
                # No need to recreate the SQLite DB. SQLite will
                # create it for us if it's not there...
            elif _is_mysql(conn_string) or _is_postgresql(conn_string):
                # We can execute the MySQL client to destroy and re-create
                # the MYSQL database, which is easier and less error-prone
                # than using SQLAlchemy to do this via MetaData...trust me.
                database = conn_pieces.path.strip('/')
                loc_pieces = conn_pieces.netloc.split('@')
                host = loc_pieces[1]
                auth_pieces = loc_pieces[0].split(':')
                user = auth_pieces[0]
                password = ""
                if len(auth_pieces) > 1:
                    if auth_pieces[1].strip():
                        password = "-p%s" % auth_pieces[1]
                if _is_mysql(conn_string):
                    sql = ("drop database if exists %(database)s; "
                           "create database %(database)s;") % locals()
                    cmd = ("mysql -u%(user)s %(password)s -h%(host)s "
                           "-e\"%(sql)s\"") % locals()
                if _is_postgresql(conn_string):
                    cmd = ("dropdb %(database)s ; "
                           "createdb %(database)s -O %(user)s ; "
                           "echo \"select 'drop table if exists ' "
                           "|| tablename || ' cascade;' "
                           "from pg_tables where schemaname = 'public';\" | "
                           "psql -d %(database)s | grep '^\s*drop' | "
                           "psql -d %(database)s") % locals()
                exitcode, out, err = utils.execute(cmd)
                self.assertEqual(0, exitcode)

    def test_walk_versions(self):
        """
        Walks all version scripts for each tested database, ensuring
        that there are no errors in the version scripts for each engine
        """
        for key, engine in self.engines.items():
            self.config(sql_connection=TestMigrations.TEST_DATABASES[key])
            self._walk_versions()

    def test_version_control_existing_db(self):
        """
        Creates a DB without version control information, places it
        under version control and checks that it can be upgraded
        without errors.
        """
        for key, engine in self.engines.items():
            self.config(sql_connection=TestMigrations.TEST_DATABASES[key])
            self._create_unversioned_001_db(engine)
            self._walk_versions(initial_version=1)

    def _create_unversioned_001_db(self, engine):
        # Create the initial version of the images table
        meta = MetaData()
        meta.bind = engine
        images_001 = Table('images',
                           meta,
                           Column('id', models.Integer, primary_key=True),
                           Column('name', String(255)),
                           Column('type', String(30)),
                           Column('size', Integer),
                           Column('status', String(30)),
                           Column('is_public', Boolean, default=False),
                           Column('location', Text),
                           Column('created_at', DateTime(), nullable=False),
                           Column('updated_at', DateTime()),
                           Column('deleted_at', DateTime()),
                           Column('deleted',
                                  Boolean(),
                                  nullable=False,
                                  default=False))
        images_001.create()

    def _walk_versions(self, initial_version=0):
        # Determine latest version script from the repo, then
        # upgrade from 1 through to the latest, with no data
        # in the databases. This just checks that the schema itself
        # upgrades successfully.

        # Assert we are not under version control...
        self.assertRaises(exception.DatabaseMigrationError,
                          migration_api.db_version)
        # Place the database under version control
        migration_api.version_control(version=initial_version)

        cur_version = migration_api.db_version()
        self.assertEqual(initial_version, cur_version)

        for version in xrange(initial_version + 1,
                              TestMigrations.REPOSITORY.latest + 1):
            migration_api.db_sync(version)
            cur_version = migration_api.db_version()
            self.assertEqual(cur_version, version)

        # Now walk it back down to 0 from the latest, testing
        # the downgrade paths.
        for version in reversed(xrange(0, TestMigrations.REPOSITORY.latest)):
            migration_api.downgrade(version)
            cur_version = migration_api.db_version()
            self.assertEqual(cur_version, version)

    def test_db_sync(self):
        initial_version = 0
        migration_api.db_sync(initial_version)
        cur_version = migration_api.db_version()
        self.assertEqual(cur_version, initial_version)

        migration_api.db_sync(TestMigrations.REPOSITORY.latest)
        cur_version = migration_api.db_version()
        self.assertEqual(cur_version, TestMigrations.REPOSITORY.latest)

        migration_api.db_sync(initial_version)
        cur_version = migration_api.db_version()
        self.assertEqual(cur_version, initial_version)

    def test_no_data_loss_2_to_3_to_2(self):
        """
        Here, we test that in the case when we moved a column "type" from the
        base images table to be records in the image_properties table, that
        we don't lose any data during the migration. Similarly, we test that
        on downgrade, we don't lose any data, as the records are moved from
        the image_properties table back into the base image table.
        """
        for key, engine in self.engines.items():
            self.config(sql_connection=TestMigrations.TEST_DATABASES[key])
            self._no_data_loss_2_to_3_to_2(engine)

    def _no_data_loss_2_to_3_to_2(self, engine):
        migration_api.version_control(version=0)
        migration_api.upgrade(2)

        cur_version = migration_api.db_version()
        self.assertEquals(2, cur_version)

        # We are now on version 2. Check that the images table does
        # not contain the type column...

        images_table = Table('images', MetaData(), autoload=True,
                             autoload_with=engine)

        image_properties_table = Table('image_properties', MetaData(),
                                       autoload=True,
                                       autoload_with=engine)

        self.assertTrue('type' in images_table.c,
                        "'type' column found in images table columns! "
                        "images table columns: %s"
                        % images_table.c.keys())

        conn = engine.connect()
        sel = select([func.count("*")], from_obj=[images_table])
        orig_num_images = conn.execute(sel).scalar()
        sel = select([func.count("*")], from_obj=[image_properties_table])
        orig_num_image_properties = conn.execute(sel).scalar()

        now = datetime.datetime.now()
        inserter = images_table.insert()
        conn.execute(inserter, [
                {'deleted': False, 'created_at': now,
                 'updated_at': now, 'type': 'kernel',
                 'status': 'active', 'is_public': True},
                {'deleted': False, 'created_at': now,
                 'updated_at': now, 'type': 'ramdisk',
                 'status': 'active', 'is_public': True}])

        sel = select([func.count("*")], from_obj=[images_table])
        num_images = conn.execute(sel).scalar()
        self.assertEqual(orig_num_images + 2, num_images)
        conn.close()

        # Now let's upgrade to 3. This should move the type column
        # to the image_properties table as type properties.

        migration_api.upgrade(3)

        cur_version = migration_api.db_version()
        self.assertEquals(3, cur_version)

        images_table = Table('images', MetaData(), autoload=True,
                             autoload_with=engine)

        self.assertTrue('type' not in images_table.c,
                        "'type' column not found in images table columns! "
                        "images table columns reported by metadata: %s\n"
                        % images_table.c.keys())

        image_properties_table = Table('image_properties', MetaData(),
                                       autoload=True,
                                       autoload_with=engine)

        conn = engine.connect()
        sel = select([func.count("*")], from_obj=[image_properties_table])
        num_image_properties = conn.execute(sel).scalar()
        self.assertEqual(orig_num_image_properties + 2, num_image_properties)
        conn.close()

        # Downgrade to 2 and check that the type properties were moved
        # to the main image table

        migration_api.downgrade(2)

        images_table = Table('images', MetaData(), autoload=True,
                             autoload_with=engine)

        self.assertTrue('type' in images_table.c,
                        "'type' column found in images table columns! "
                        "images table columns: %s"
                        % images_table.c.keys())

        image_properties_table = Table('image_properties', MetaData(),
                                       autoload=True,
                                       autoload_with=engine)

        conn = engine.connect()
        sel = select([func.count("*")], from_obj=[image_properties_table])
        last_num_image_properties = conn.execute(sel).scalar()

        self.assertEqual(num_image_properties - 2, last_num_image_properties)

    def test_no_data_loss_14_to_15(self):
        for key, engine in self.engines.items():
            self.config(sql_connection=TestMigrations.TEST_DATABASES[key])
            self._check_no_data_loss_14_to_15(engine)

    def _check_no_data_loss_14_to_15(self, engine):
        """
        Check that migrating swift location credentials to quoted form
        and back does not result in data loss.
        """
        migration_api.version_control(version=0)
        migration_api.upgrade(14)

        conn = engine.connect()
        images_table = Table('images', MetaData(), autoload=True,
                             autoload_with=engine)

        def get_locations():
            conn = engine.connect()
            locations = [x[0] for x in
                         conn.execute(
                             select(['location'], from_obj=[images_table]))]
            conn.close()
            return locations

        unquoted_locations = [
            'swift://acct:usr:pass@example.com/container/obj-id',
            'file://foo',
        ]
        quoted_locations = [
            'swift://acct%3Ausr:pass@example.com/container/obj-id',
            'file://foo',
        ]

        # Insert images with an unquoted image location
        now = datetime.datetime.now()
        kwargs = dict(deleted=False,
                      created_at=now,
                      updated_at=now,
                      status='active',
                      is_public=True,
                      min_disk=0,
                      min_ram=0)
        for i, location in enumerate(unquoted_locations):
            kwargs.update(location=location, id=i)
            conn.execute(images_table.insert(), [kwargs])
        conn.close()

        migration_api.upgrade(15)

        self.assertEqual(get_locations(), quoted_locations)

        migration_api.downgrade(14)

        self.assertEqual(get_locations(), unquoted_locations)

    def test_no_data_loss_15_to_16_to_15(self):
        """
        Here, we test that in the case when we moved a column "type" from the
        base images table to be records in the image_properties table, that
        we don't lose any data during the migration. Similarly, we test that
        on downgrade, we don't lose any data, as the records are moved from
        the image_properties table back into the base image table.
        """
        for key, engine in self.engines.items():
            self.config(sql_connection=TestMigrations.TEST_DATABASES[key])
            self._no_data_loss_15_to_16_to_15(engine)

    def _no_data_loss_15_to_16_to_15(self, engine):
        migration_api.version_control(version=0)
        migration_api.upgrade(15)

        cur_version = migration_api.db_version()
        self.assertEquals(15, cur_version)

        # We are now on version 15.

        image_members_table = Table('image_members', MetaData(),
                                    autoload=True, autoload_with=engine)

        self.assertTrue('status' not in image_members_table.c,
                        "'status' not column found in image_members table "
                        "columns! image_members table columns: %s"
                        % image_members_table.c.keys())

        conn = engine.connect()
        sel = select([func.count("*")], from_obj=[image_members_table])
        orig_num_image_members = conn.execute(sel).scalar()

        now = datetime.datetime.now()
        inserter = image_members_table.insert()
        conn.execute(inserter, [
                {'deleted': False, 'created_at': now, 'member': 'fake-member',
                 'updated_at': now, 'can_share': False,
                 'image_id': 'fake-image-id1'}])

        sel = select([func.count("*")], from_obj=[image_members_table])
        num_image_members = conn.execute(sel).scalar()
        self.assertEqual(orig_num_image_members + 1, num_image_members)
        conn.close()

        #Upgrade to version 16

        migration_api.upgrade(16)

        cur_version = migration_api.db_version()
        self.assertEquals(16, cur_version)

        image_members_table = Table('image_members', MetaData(),
                                    autoload=True, autoload_with=engine)

        self.assertTrue('status' in image_members_table.c,
                        "'status' column found in image_members table "
                        "columns! image_members table columns: %s"
                        % image_members_table.c.keys())

        conn = engine.connect()
        sel = select([func.count("*")], from_obj=[image_members_table])
        num_image_members = conn.execute(sel).scalar()
        self.assertEqual(orig_num_image_members + 1, num_image_members)

        now = datetime.datetime.now()
        inserter = image_members_table.insert()
        conn.execute(inserter, [
                {'deleted': False, 'created_at': now, 'member': 'fake-member',
                 'updated_at': now, 'can_share': False, 'status': 'pending',
                 'image_id': 'fake-image-id2'}])

        sel = select([func.count("*")], from_obj=[image_members_table])
        num_image_members = conn.execute(sel).scalar()
        self.assertEqual(orig_num_image_members + 2, num_image_members)
        conn.close()

        #Downgrade to version 15

        migration_api.downgrade(15)

        cur_version = migration_api.db_version()
        self.assertEquals(15, cur_version)

        image_members_table = Table('image_members', MetaData(),
                                    autoload=True, autoload_with=engine)

        self.assertTrue('status' not in image_members_table.c,
                        "'status' column not found in image_members table "
                        "columns! image_members table columns: %s"
                        % image_members_table.c.keys())

        conn = engine.connect()
        sel = select([func.count("*")], from_obj=[image_members_table])
        num_image_members = conn.execute(sel).scalar()
        self.assertEqual(orig_num_image_members + 2, num_image_members)
        conn.close()

    def test_quote_encrypted_locations_16_to_17(self):
        self.metadata_encryption_key = 'a' * 16
        for key, engine in self.engines.items():
            self.config(sql_connection=TestMigrations.TEST_DATABASES[key])
            self.config(metadata_encryption_key=self.metadata_encryption_key)
            self._check_16_to_17(engine)

    def _check_16_to_17(self, engine):
        """
        Check that migrating swift location credentials to quoted form
        and back works.
        """
        migration_api.version_control(version=0)
        migration_api.upgrade(16)

        conn = engine.connect()
        images_table = Table('images', MetaData(), autoload=True,
                             autoload_with=engine)

        def get_locations():
            conn = engine.connect()
            locations = [x[0] for x in
                         conn.execute(
                             select(['location'], from_obj=[images_table]))]
            conn.close()
            return locations

        unquoted = 'swift://acct:usr:pass@example.com/container/obj-id'
        encrypted_unquoted = crypt.urlsafe_encrypt(
                                    self.metadata_encryption_key,
                                    unquoted, 64)

        quoted = 'swift://acct%3Ausr:pass@example.com/container/obj-id'

        # Insert image with an unquoted image location
        now = datetime.datetime.now()
        kwargs = dict(deleted=False,
                      created_at=now,
                      updated_at=now,
                      status='active',
                      is_public=True,
                      min_disk=0,
                      min_ram=0)
        kwargs.update(location=encrypted_unquoted, id=1)
        conn.execute(images_table.insert(), [kwargs])
        conn.close()

        migration_api.upgrade(17)

        actual_location = crypt.urlsafe_decrypt(self.metadata_encryption_key,
                                                get_locations()[0])

        self.assertEqual(actual_location, quoted)

        migration_api.downgrade(16)

        actual_location = crypt.urlsafe_decrypt(self.metadata_encryption_key,
                                                get_locations()[0])

        self.assertEqual(actual_location, unquoted)

    def test_no_data_loss_16_to_17(self):
        self.metadata_encryption_key = 'a' * 16
        for key, engine in self.engines.items():
            self.config(sql_connection=TestMigrations.TEST_DATABASES[key])
            self.config(metadata_encryption_key=self.metadata_encryption_key)
            self._check_no_data_loss_16_to_17(engine)

    def _check_no_data_loss_16_to_17(self, engine):
        """
        Check that migrating swift location credentials to quoted form
        and back does not result in data loss.
        """
        migration_api.version_control(version=0)
        migration_api.upgrade(16)

        conn = engine.connect()
        images_table = Table('images', MetaData(), autoload=True,
                             autoload_with=engine)

        def get_locations():
            conn = engine.connect()
            locations = [x[0] for x in
                         conn.execute(
                             select(['location'], from_obj=[images_table]))]
            conn.close()
            return locations

        locations = ['file://ab',
                     'file://abc',
                     'swift://acct3A%foobar:pass@example.com/container/obj-id']

        # Insert images with an unquoted image location
        now = datetime.datetime.now()
        kwargs = dict(deleted=False,
                      created_at=now,
                      updated_at=now,
                      status='active',
                      is_public=True,
                      min_disk=0,
                      min_ram=0)
        for i, location in enumerate(locations):
            kwargs.update(location=location, id=i)
            conn.execute(images_table.insert(), [kwargs])
        conn.close()

        def assert_locations():
            actual_locations = get_locations()
            for location in locations:
                if not location in actual_locations:
                    self.fail(_("location: %s data lost") % location)

        migration_api.upgrade(17)

        assert_locations()

        migration_api.downgrade(16)

        assert_locations()

    def test_migration_19(self):
        for key, engine in self.engines.items():
            self.config(sql_connection=TestMigrations.TEST_DATABASES[key])

            migration_api.version_control(version=0)
            migration_api.upgrade(18)

            images_table = Table('images', MetaData(engine), autoload=True)

            now = datetime.datetime.now()
            base_values = {
                'deleted': False,
                'created_at': now,
                'updated_at': now,
                'status': 'active',
                'is_public': True,
                'min_disk': 0,
                'min_ram': 0,
            }
            images = [
                {'id': 1, 'location': 'http://glance.example.com'},
                #NOTE(bcwaldon): images with a location of None should
                # not be migrated
                {'id': 2, 'location': None},
            ]
            map(lambda image: image.update(base_values), images)
            for image in images:
                images_table.insert().values(image).execute()

            migration_api.upgrade(19)

            image_locations_table = Table('image_locations', MetaData(engine),
                                          autoload=True)
            records = image_locations_table.select().execute().fetchall()

            self.assertEqual(len(records), 1)
            locations = dict([(il.image_id, il.value) for il in records])
            self.assertEqual({'1': 'http://glance.example.com'}, locations)

            image_locations_table = Table('image_locations', MetaData(engine),
                                          autoload=True)
            image_locations_table.update()\
                                 .where(image_locations_table.c.image_id == 1)\
                                 .values(value='http://swift.example.com')\
                                 .execute()

            migration_api.downgrade(18)

            images_table = Table('images', MetaData(engine), autoload=True)
            records = images_table.select().execute().fetchall()

            self.assertEqual(len(records), 2)
            locations = dict([(i.id, i.location) for i in records])
            self.assertEqual({'1': 'http://swift.example.com', '2': None},
                             locations)

    def test_migration_20(self):
        for key, engine in self.engines.items():
            self.config(sql_connection=TestMigrations.TEST_DATABASES[key])

            migration_api.version_control(version=0)
            migration_api.upgrade(19)

            images_table = Table('images', MetaData(engine), autoload=True)
            self.assertTrue('location' in images_table.c)

            migration_api.upgrade(20)

            images_table = Table('images', MetaData(engine), autoload=True)
            self.assertFalse('location' in images_table.c)

            migration_api.downgrade(19)

            images_table = Table('images', MetaData(engine), autoload=True)
            self.assertTrue('location' in images_table.c)
