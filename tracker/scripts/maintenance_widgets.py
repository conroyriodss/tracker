"""Explicit selection and hash-bound review of existing sealed cleanup plans."""
from __future__ import annotations

import json
from pathlib import Path


class CleanupPlanReview:
    """Review state is invalidated by refresh, selection change, or application."""

    def __init__(self, project: Path):
        import step_maintenance as maintenance
        self.maintenance = maintenance
        self.project = Path(project).resolve()
        if self.project != Path(maintenance.PROJECT).resolve():
            raise RuntimeError("Maintenance module uses a different project; restart the runtime.")
        self.clear()

    def clear(self):
        self.path = None
        self.sha256 = ""

    def _load(self, path):
        path = Path(path).resolve()
        plan = self.maintenance.load_cleanup_plan(path)
        receipt_path = path.parent / "cleanup_receipt.json"
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if receipt.get("status") in {"committed", "partial_failure"}:
                raise ValueError("This plan has already been applied; create a new plan.")
        return plan

    def discover(self):
        self.clear()
        options, errors = [], []
        for path in sorted((self.project / "runs/maintenance").glob("*/cleanup_plan.json"), reverse=True):
            try:
                plan = self._load(path)
                label = (f"{path.parent.name} | {plan['candidate_count']} paths | "
                         f"{plan['candidate_bytes']:,} bytes")
                options.append((label, str(path)))
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                errors.append(f"{path}: {exc}")
        return options, errors

    def inspect(self, selected):
        self.clear()
        if not selected:
            raise ValueError("Select a saved plan, or create one first.")
        path = Path(selected).resolve()
        before = self.maintenance.sha256_file(path)
        plan = self._load(path)
        after = self.maintenance.sha256_file(path)
        if not before or before != after:
            raise RuntimeError("Plan changed during inspection; refresh and inspect again.")
        self.path, self.sha256 = str(path), after
        return plan

    def apply_arguments(self, selected, *, reviewed, no_active_writers):
        if not selected or str(Path(selected).resolve()) != self.path or not self.sha256:
            raise ValueError("Inspect the selected plan before applying it.")
        if not reviewed or not no_active_writers:
            raise ValueError("Acknowledge the reviewed plan and confirm that all other writers stopped.")
        self._load(selected)
        if self.maintenance.sha256_file(selected) != self.sha256:
            self.clear()
            raise RuntimeError("Plan changed since review; inspect it again.")
        arguments = ["cleanup", "--plan", self.path, "--reviewed-sha256", self.sha256,
                     "--apply", "--confirm", self.maintenance.CLEANUP_CONFIRMATION]
        self.clear()
        return arguments


class CleanupControls:
    def __init__(self, runtime, *, keep_runs=5, remove_temporary=False):
        import ipywidgets as widgets
        from IPython.display import display
        self.runtime = runtime
        self.review = CleanupPlanReview(runtime.project)
        self.keep_runs, self.remove_temporary = keep_runs, remove_temporary
        self.output = widgets.Output()
        self.selector = widgets.Dropdown(options=[("Select a saved plan", "")],
                                         description="Plan:", layout=widgets.Layout(width="100%"))
        self.create = widgets.Button(description="Create plan")
        self.refresh = widgets.Button(description="Refresh list")
        self.inspect = widgets.Button(description="Inspect selected plan")
        self.apply = widgets.Button(description="Apply reviewed plan", button_style="danger", disabled=True)
        self.reviewed = widgets.Checkbox(value=False, description="I reviewed this plan's eligible and protected paths.", indent=False, layout=widgets.Layout(width="100%"))
        self.no_writers = widgets.Checkbox(value=False, description="All other notebooks and background writers are stopped.", indent=False, layout=widgets.Layout(width="100%"))
        self.selector.observe(self._selection_changed, names="value")
        for box in (self.reviewed, self.no_writers):
            box.observe(self._enable_apply, names="value")
        self.create.on_click(self._create)
        self.refresh.on_click(self._refresh)
        self.inspect.on_click(self._inspect)
        self.apply.on_click(self._apply)
        self.widget = widgets.VBox([widgets.HBox([self.create, self.refresh]), self.selector,
                                    self.inspect, self.output, self.reviewed, self.no_writers, self.apply])
        self._refresh()
        display(self.widget)

    def _selection_changed(self, _=None):
        self.review.clear()
        self.reviewed.value = self.no_writers.value = False
        self.apply.disabled = True
        self.output.clear_output()

    def _enable_apply(self, _=None):
        self.apply.disabled = not (self.review.path == self.selector.value and self.review.sha256
                                   and self.reviewed.value and self.no_writers.value)

    def _refresh(self, _=None):
        self._selection_changed()
        options, errors = self.review.discover()
        self.selector.options = [("Select a saved plan", "")] + options
        self.selector.value = ""
        with self.output:
            print(f"{len(options)} unapplied sealed plan(s). Select one, then inspect it.")
            if not options:
                print("Click Create plan. Creating a plan does not delete files.")
            for error in errors:
                print(f"Not selectable: {error}")

    def _create(self, _=None):
        self._selection_changed()
        arguments = ["cleanup", "--keep-runs", str(self.keep_runs)]
        if self.remove_temporary:
            arguments.append("--remove-temporary")
        with self.output:
            result = self.runtime.run_script("step_maintenance.py", arguments, label="maintenance_cleanup_plan")
            if not result.ok:
                print(f"Plan creation failed; inspect {result.log_path}")
                return
        self._refresh()

    def _inspect(self, _=None):
        self._selection_changed()
        with self.output:
            try:
                plan = self.review.inspect(self.selector.value)
                print(f"Exact plan: {self.review.path}\nSHA-256: {self.review.sha256}")
                print(json.dumps(plan, indent=2))
                print("Inspection only: no files were deleted. Review all paths above before checking the acknowledgments.")
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                print(f"Cannot inspect: {exc}")

    def _apply(self, _=None):
        self.apply.disabled = True
        with self.output:
            try:
                arguments = self.review.apply_arguments(self.selector.value, reviewed=self.reviewed.value,
                                                        no_active_writers=self.no_writers.value)
                result = self.runtime.run_script("step_maintenance.py", arguments, label="maintenance_cleanup_apply")
                print("Cleanup completed." if result.ok else f"Cleanup failed; inspect {result.log_path}")
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                print(f"Cleanup not started: {exc}")
            finally:
                self.review.clear()
                self.reviewed.value = self.no_writers.value = False
                self.apply.disabled = True


def launch_cleanup_controls(runtime, *, keep_runs=5, remove_temporary=False):
    return CleanupControls(runtime, keep_runs=keep_runs, remove_temporary=remove_temporary)
