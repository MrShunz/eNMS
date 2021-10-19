from collections import defaultdict
from heapq import heappop, heappush
from sqlalchemy import Boolean, ForeignKey, Integer, or_
from sqlalchemy.orm import aliased, backref, relationship
from sqlalchemy.schema import UniqueConstraint

from eNMS.database import db
from eNMS.models.base import AbstractBase
from eNMS.forms import ServiceForm
from eNMS.fields import BooleanField, HiddenField, InstanceField, SelectField
from eNMS.models.automation import Service
from eNMS.runner import Runner
from eNMS.variables import vs


class Workflow(Service):

    __tablename__ = "workflow"
    pretty_name = "Workflow"
    parent_type = "service"
    id = db.Column(Integer, ForeignKey("service.id"), primary_key=True)
    close_connection = db.Column(Boolean, default=False)
    labels = db.Column(db.Dict, info={"log_change": False})
    services = relationship(
        "Service", secondary=db.service_workflow_table, back_populates="workflows"
    )
    edges = relationship(
        "WorkflowEdge", back_populates="workflow", cascade="all, delete-orphan"
    )
    superworkflow_id = db.Column(
        Integer, ForeignKey("workflow.id", ondelete="SET NULL")
    )
    superworkflow = relationship(
        "Workflow", remote_side=[id], foreign_keys="Workflow.superworkflow_id"
    )

    __mapper_args__ = {"polymorphic_identity": "workflow"}

    def __init__(self, **kwargs):
        migration_import = kwargs.get("migration_import", False)
        if not migration_import:
            start = db.fetch("service", scoped_name="Start", rbac=None)
            end = db.fetch("service", scoped_name="End", rbac=None)
            self.services.extend([start, end])
        super().__init__(**kwargs)
        if not migration_import and self.name not in end.positions:
            end.positions[self.name] = (500, 0)

    def delete(self):
        for service in self.services:
            if not service.shared:
                db.delete_instance(service)

    def set_name(self, name=None):
        old_name = self.name
        super().set_name(name)
        for service in self.services:
            if not service.shared:
                service.set_name()
            if old_name in service.positions:
                service.positions[self.name] = service.positions[old_name]
        for edge in self.edges:
            edge.name.replace(old_name, self.name)

    def duplicate(self, workflow=None, clone=None):
        if not clone:
            clone = super().duplicate(workflow)
        clone.labels = self.labels
        clone_services = {}
        db.session.commit()
        for service in self.services:
            if service.shared:
                service_clone = service
                if service not in clone.services:
                    clone.services.append(service)
            else:
                service_clone = service.duplicate(clone)
            service_clone.positions[clone.name] = service.positions.get(
                self.name, (0, 0)
            )
            service_clone.skip[clone.name] = service.skip.get(self.name, False)
            clone_services[service.id] = service_clone
        db.session.commit()
        for edge in self.edges:
            clone.edges.append(
                db.factory(
                    "workflow_edge",
                    **{
                        "workflow": clone.id,
                        "subtype": edge.subtype,
                        "source": clone_services[edge.source.id].id,
                        "destination": clone_services[edge.destination.id].id,
                    },
                )
            )
            db.session.commit()
        return clone

    @property
    def deep_services(self):
        services = [
            service.deep_services if service.type == "workflow" else [service]
            for service in self.services
        ]
        return [self] + sum(services, [])

    @property
    def deep_edges(self):
        return sum([w.edges for w in self.deep_services if w.type == "workflow"], [])

    def job(self, run, device=None):
        number_of_runs = defaultdict(int)
        start = db.fetch("service", scoped_name="Start")
        end = db.fetch("service", scoped_name="End")
        services, targets = [], defaultdict(set)
        start_targets = [device] if device else run.target_devices
        for service_id in run.start_services or [start.id]:
            service = db.fetch("service", id=service_id)
            targets[service.name] |= {device.name for device in start_targets}
            heappush(services, (1 / service.priority, service))
        visited, restart_run = set(), run.restart_run
        tracking_bfs = run.run_method == "per_service_with_workflow_targets"
        while services:
            if run.stop:
                return {"payload": run.payload, "success": False, "result": "Aborted"}
            _, service = heappop(services)
            if number_of_runs[service.name] >= service.maximum_runs:
                continue
            number_of_runs[service.name] += 1
            visited.add(service)
            if service in (start, end) or service.skip.get(self.name, False):
                success = service.skip_value == "success"
                results = {"result": "skipped", "success": success}
                if tracking_bfs or device:
                    results["summary"] = {
                        "success": targets[service.name],
                        "failure": [],
                    }
            else:
                kwargs = {
                    "service": run.placeholder
                    if service.scoped_name == "Placeholder"
                    else service,
                    "workflow": self,
                    "restart_run": restart_run,
                    "parent": run,
                    "parent_runtime": run.parent_runtime,
                    "workflow_run_method": run.run_method,
                }
                if tracking_bfs or device:
                    kwargs["target_devices"] = [
                        db.fetch("device", name=name) for name in targets[service.name]
                    ]
                if run.parent_device:
                    kwargs["parent_device"] = run.parent_device
                results = Runner(run, payload=run.payload, **kwargs).results
                if not results:
                    continue
            status = "success" if results["success"] else "failure"
            summary = results.get("summary", {})
            if not tracking_bfs and not device:
                run.write_state(f"progress/service/{status}", 1, "increment")
            for edge_type in ("success", "failure"):
                if not tracking_bfs and edge_type != status:
                    continue
                if tracking_bfs and not summary[edge_type]:
                    continue
                for successor, edge in service.neighbors(
                    self, "destination", edge_type
                ):
                    if tracking_bfs or device:
                        targets[successor.name] |= set(summary[edge_type])
                    heappush(services, ((1 / successor.priority, successor)))
                    if tracking_bfs or device:
                        run.write_state(
                            f"edges/{edge.id}", len(summary[edge_type]), "increment"
                        )
                    else:
                        run.write_state(f"edges/{edge.id}", "DONE")
        if tracking_bfs or device:
            failed = list(targets[start.name] - targets[end.name])
            summary = {"success": list(targets[end.name]), "failure": failed}
            results = {
                "payload": run.payload,
                "success": not failed,
                "summary": summary,
            }
        else:
            results = {"payload": run.payload, "success": end in visited}
        run.restart_run = restart_run
        return results


class WorkflowForm(ServiceForm):
    form_type = HiddenField(default="workflow")
    close_connection = BooleanField(default=False)
    run_method = SelectField(
        "Run Method",
        choices=(
            ("per_device", "Run the workflow device by device"),
            (
                "per_service_with_workflow_targets",
                "Run the workflow service by service using workflow targets",
            ),
            (
                "per_service_with_service_targets",
                "Run the workflow service by service using service targets",
            ),
        ),
    )
    superworkflow = InstanceField("Superworkflow")


class WorkflowEdge(AbstractBase):

    __tablename__ = type = class_type = "workflow_edge"
    id = db.Column(Integer, primary_key=True)
    name = db.Column(db.SmallString, unique=True)
    label = db.Column(db.SmallString)
    color = db.Column(db.SmallString)
    subtype = db.Column(db.SmallString)
    source_id = db.Column(Integer, ForeignKey("service.id"))
    source = relationship(
        "Service",
        primaryjoin="Service.id == WorkflowEdge.source_id",
        backref=backref("destinations", cascade="all, delete-orphan"),
        foreign_keys="WorkflowEdge.source_id",
    )
    destination_id = db.Column(Integer, ForeignKey("service.id"))
    destination = relationship(
        "Service",
        primaryjoin="Service.id == WorkflowEdge.destination_id",
        backref=backref("sources", cascade="all, delete-orphan"),
        foreign_keys="WorkflowEdge.destination_id",
    )
    workflow_id = db.Column(Integer, ForeignKey("workflow.id"))
    workflow = relationship(
        "Workflow", back_populates="edges", foreign_keys="WorkflowEdge.workflow_id"
    )
    __table_args__ = (
        UniqueConstraint(subtype, source_id, destination_id, workflow_id),
    )

    def __init__(self, **kwargs):
        self.label = kwargs["subtype"]
        self.color = "green" if kwargs["subtype"] == "success" else "red"
        super().__init__(**kwargs)

    def update(self, **kwargs):
        super().update(**kwargs)
        self.set_name(kwargs.get("name"))

    @classmethod
    def rbac_filter(cls, query, mode, user):
        originals_alias = aliased(vs.models["service"])
        if mode == "edit":
            query = (
                query.join(cls.workflow)
                .join(originals_alias, vs.models["service"].originals)
                .join(vs.models["user"], originals_alias.owners)
                .filter(
                    or_(
                        vs.models["user"].name == user.name,
                        ~originals_alias.lock_mode.contains(mode),
                    )
                )
            )
        return query

    def set_name(self, name=None):
        self.name = name or f"[{self.workflow}] {vs.get_time()}"
