import mysql.connector
import config


def connect_old():
    return mysql.connector.connect(**config.get_old_db_config())


def connect_new():
    return mysql.connector.connect(**config.get_new_db_config())


def connect_migration():
    return mysql.connector.connect(**config.get_migration_db_config())
