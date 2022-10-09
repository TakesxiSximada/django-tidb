# Copyright 2021 PingCAP, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# See the License for the specific language governing permissions and
# limitations under the License.

from django.db.backends.utils import split_identifier
from django.db.backends.mysql.schema import (
    DatabaseSchemaEditor as MysqlDatabaseSchemaEditor,
)


class DatabaseSchemaEditor(MysqlDatabaseSchemaEditor):
    @property
    def sql_delete_check(self):
        return 'ALTER TABLE %(table)s DROP CHECK %(name)s'

    @property
    def sql_rename_column(self):
        return 'ALTER TABLE %(table)s CHANGE %(old_column)s %(new_column)s %(type)s'

    def skip_default_on_alter(self, field):
        return False

    @property
    def _supports_limited_data_type_defaults(self):
        return True

    def _field_should_be_indexed(self, model, field):
        return False

    def add_field(self, model, field):
        """
        Create a field on a model. Usually involves adding a column, but may
        involve adding a table instead (for M2M fields).
        """
        # Special-case implicit M2M tables
        if field.many_to_many and field.remote_field.through._meta.auto_created:
            return self.create_model(field.remote_field.through)
        # Get the column's definition
        definition, params = self.column_sql(model, field, include_default=True)
        # It might not actually have a column behind it
        if definition is None:
            return
        # Check constraints can go on the column SQL here
        db_params = field.db_parameters(connection=self.connection)
        if db_params["check"]:
            definition += " " + self.sql_check_constraint % db_params
        if (
            field.remote_field
            and self.connection.features.supports_foreign_keys
            and field.db_constraint
        ):
            constraint_suffix = "_fk_%(to_table)s_%(to_column)s"
            # Add FK constraint inline, if supported.
            if self.sql_create_column_inline_fk:
                to_table = field.remote_field.model._meta.db_table
                to_column = field.remote_field.model._meta.get_field(
                    field.remote_field.field_name
                ).column
                namespace, _ = split_identifier(model._meta.db_table)
                definition += " " + self.sql_create_column_inline_fk % {
                    "name": self._fk_constraint_name(model, field, constraint_suffix),
                    "namespace": "%s." % self.quote_name(namespace)
                    if namespace
                    else "",
                    "column": self.quote_name(field.column),
                    "to_table": self.quote_name(to_table),
                    "to_column": self.quote_name(to_column),
                    "deferrable": self.connection.ops.deferrable_sql(),
                }
            # Otherwise, add FK constraints later.
            else:
                self.deferred_sql.append(
                    self._create_fk_sql(model, field, constraint_suffix)
                )

        # Build the SQL and run it
        sql = self.sql_create_column % {
            "table": self.quote_name(model._meta.db_table),
            "column": self.quote_name(field.column),
            "definition": definition,
        }
        self.execute(sql, params)

        # Drop the default if we need to
        # (Django usually does not use in-database defaults)
        if (
            not self.skip_default_on_alter(field)
            and self.effective_default(field) is not None
        ):
            changes_sql, params = self._alter_column_default_sql(
                model, None, field, drop=True
            )
            sql = self.sql_alter_column % {
                "table": self.quote_name(model._meta.db_table),
                "changes": changes_sql,
            }
            self.execute(sql, params)
        # Add an index, if required
        self.deferred_sql.extend(self._field_indexes_sql(model, field))
        # Reset connection if required
        if self.connection.features.connection_persists_old_columns:
            self.connection.close()
