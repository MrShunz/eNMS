from sqlalchemy.ext.mutable import MutableDict, MutableList

from eNMS.database import db
from eNMS.environment import env
from eNMS.variables import vs


class AbstractBase(db.base):

    __abstract__ = True
    pool_model = False
    model_properties = {}

    def __init__(self, **kwargs):
        self.update(**kwargs)

    def __lt__(self, other):
        return True

    def __repr__(self):
        return getattr(self, "name", str(self.id))

    def __getattribute__(self, property):
        if property in vs.private_properties_set:
            value = super().__getattribute__(property)
            return value
        else:
            return super().__getattribute__(property)

    def __setattr__(self, property, value):
        if property in vs.private_properties_set:
            if not value:
                return
            value = env.encrypt_password(value).decode("utf-8")
            super().__setattr__(property, value)
        else:
            super().__setattr__(property, value)

    @classmethod
    def filtering_constraints(cls, **_):
        return []

    @property
    def ui_name(self):
        return self.name

    @property
    def base_properties(self):
        return {prop: getattr(self, prop) for prop in ("id", "name", "type")}

    def update(self, rbac="read", **kwargs):
        relation = vs.relationships[self.__tablename__]
        for property, value in kwargs.items():
            if not hasattr(self, property):
                continue
            property_type = vs.model_properties[self.__tablename__].get(property, None)
            if property in relation:
                if relation[property]["list"]:
                    value = db.objectify(relation[property]["model"], value)
                elif value:
                    value = db.fetch(relation[property]["model"], id=value, rbac=rbac)
            if property_type == "bool":
                value = value not in (False, "false")
            setattr(self, property, value)
        if not kwargs.get("update_pools") or not self.pool_model:
            return
        for pool in db.fetch_all("pool", rbac=None):
            if pool.manually_defined:
                continue
            match = pool.match_instance(self)
            if match and self not in getattr(pool, f"{self.class_type}s"):
                getattr(pool, f"{self.class_type}s").append(self)
            if self in getattr(pool, f"{self.class_type}s") and not match:
                getattr(pool, f"{self.class_type}s").remove(self)

    def delete(self):
        pass

    def get_properties(
        self, export=False, exclude=None, include=None, private_properties=False
    ):
        result = {}
        no_migrate = db.dont_migrate.get(getattr(self, "export_type", self.type), {})
        properties = list(vs.model_properties[self.type])
        for property in properties:
            if not private_properties and property in vs.private_properties_set:
                continue
            if property in db.dont_serialize.get(self.type, []):
                continue
            if export and property in getattr(self, "model_properties", {}):
                continue
            if include and property not in include or exclude and property in exclude:
                continue
            if export and property in no_migrate:
                continue
            try:
                value = getattr(self, property)
            except AttributeError:
                continue
            if export:
                if isinstance(value, MutableList):
                    value = list(value)
                if isinstance(value, MutableDict):
                    value = dict(value)
                if value is None:
                    continue
            result[property] = value
        return result

    @classmethod
    def rbac_filter(cls, query, mode, user):
        return query

    def table_properties(self, **kwargs):
        return self.get_properties(
            include=[column["data"] for column in kwargs["columns"]]
            if kwargs.get("rest_api_request")
            else None
        )

    def duplicate(self, **kwargs):
        properties = {
            property: value
            for property, value in self.get_properties().items()
            if property not in ("id", "name")
        }
        instance = db.factory(self.type, **{**properties, **kwargs})
        return instance

    def to_dict(
        self,
        export=False,
        relation_names_only=False,
        exclude=None,
        include=None,
        private_properties=False,
    ):
        properties = self.get_properties(
            export, exclude=exclude, private_properties=private_properties
        )
        no_migrate = db.dont_migrate.get(getattr(self, "export_type", self.type), {})
        for property, relation in vs.relationships[self.type].items():
            if include and property not in include or exclude and property in exclude:
                continue
            if export and property in no_migrate:
                continue
            value = getattr(self, property)
            if relation["list"]:
                properties[property] = [
                    obj.name
                    if export or relation_names_only
                    else obj.get_properties(exclude=exclude)
                    for obj in value
                ]
            else:
                if not value:
                    continue
                properties[property] = (
                    value.name
                    if export or relation_names_only
                    else value.get_properties(exclude=exclude)
                )
        return properties

    @property
    def serialized(self):
        return self.to_dict()
