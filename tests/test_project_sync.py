"""Carrying the project list between machines (CS-333).

0.1 copied `.codex-global-state.json` whole, so a laptop showed the desktop's
projects after a sync. 0.2 stopped copying it (it holds per-machine values) and
for a while carried nothing at all: the laptop kept its own sidebar. These
tests pin the replacement -- a merge, never an overwrite, never a removal.
"""
from __future__ import annotations

import json
from pathlib import Path
import unittest

from codexsync.app import ProjectsNotCarried, run_handoff, sync_projects
from codexsync.exceptions import ConfigError, SafetyPreconditionError
from codexsync.guardian_models import ValidationStatus
from codexsync.guardian_schema import validate_global_state_references
from codexsync.project_sync import (
    FOLDER_MISSING_HERE,
    ROOT_DIFFERS,
    SEVERAL_LOCAL_CANDIDATES,
    MergeKind,
    Publication,
    RootMappingAmbiguous,
    build_project_merge,
    publication_from_state,
    publication_path,
    read_board,
    root_key,
    write_publication,
)
from codexsync.safety_gate import ProcessState

try:
    from tests.test_handoff import _Workspace
except ImportError:  # collected with tests/ itself on sys.path
    from test_handoff import _Workspace


def _project(project_id: str, name: str, root: str) -> dict:
    return {"createdAt": 1, "id": project_id, "name": name, "rootPaths": [root], "updatedAt": 2}


def _state(projects: list[dict], *, order=None, pinned=(), bindings=None, extra=None) -> dict:
    state = {
        "electron-main-window-bounds": {"x": 1},
        "local-projects": {item["id"]: item for item in projects},
        "project-order": list(order if order is not None else [item["id"] for item in projects]),
        "pinned-project-ids": list(pinned),
        "thread-project-assignments": {
            thread: {"projectKind": "local", "projectId": project}
            for thread, project in (bindings or {}).items()
        },
    }
    state.update(extra or {})
    return state


def _bytes(state: dict) -> bytes:
    return json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")


def _same(_peer: str, root: str) -> str:
    return root


def _merge(local: dict, *peers: Publication, folders=lambda root: True, map_root=_same):
    return build_project_merge(
        _bytes(local), peers, machine="laptop", map_root=map_root, folder_exists=folders,
    )


class PublicationTests(unittest.TestCase):
    def test_carries_projects_order_pins_and_bindings_but_nothing_per_machine(self) -> None:
        state = _state(
            [_project("a", "Alpha", "D:\\P\\alpha"), _project("b", "Beta", "D:\\P\\beta")],
            order=["b", "a"], pinned=["a"], bindings={"t1": "a"},
        )
        publication = publication_from_state(state, "desktop")
        self.assertEqual(publication.order, ("b", "a"))
        self.assertEqual(publication.pinned, ("a",))
        self.assertEqual(publication.bindings, {"t1": "a"})
        self.assertNotIn("electron-main-window-bounds", json.dumps(publication.to_json()))

    def test_an_app_server_binding_is_carried_as_the_shared_legacy_id(self) -> None:
        state = _state([_project("a", "Alpha", "D:\\P\\alpha")], extra={
            "app-server-project-id-by-legacy-project-id-by-host": {"local:C:\\x": {"a": "srv-1"}},
        })
        state["thread-project-assignments"] = {"t1": {"projectKind": "app-server", "projectId": "srv-1"}}
        self.assertEqual(publication_from_state(state, "desktop").bindings, {"t1": "a"})

    def test_content_id_ignores_when_it_was_written(self) -> None:
        state = _state([_project("a", "Alpha", "D:\\P\\alpha")])
        first = publication_from_state(state, "desktop", now=lambda: "2026-10-01T00:00:00Z")
        second = publication_from_state(state, "desktop", now=lambda: "2026-10-02T00:00:00Z")
        self.assertEqual(first.publication_id, second.publication_id)

    def test_a_damaged_or_misplaced_file_is_not_believed(self) -> None:
        root = Path(__file__).resolve().parents[1] / "test-sandbox" / "project-board"
        self.addCleanup(lambda: [p.unlink() for p in root.glob("*.json")])
        write_publication(root, publication_from_state(_state([_project("a", "A", "D:\\a")]), "desktop"))
        good = publication_path(root, "desktop")
        (root / "laptop.json").write_text(good.read_text(encoding="utf-8"), encoding="utf-8")
        board = read_board(root)
        self.assertIn("desktop", board.publications)
        self.assertIn("laptop.json", board.unreadable)
        good.write_text(good.read_text(encoding="utf-8").replace('"A"', '"B"'), encoding="utf-8")
        self.assertIn("desktop.json", read_board(root).unreadable)


class MergeTests(unittest.TestCase):
    def test_a_project_only_the_peer_has_is_added_as_its_own_entry(self) -> None:
        local = _state([_project("l1", "Mine", "D:\\P\\mine")])
        peer = publication_from_state(_state([_project("d1", "Theirs", "D:\\P\\theirs")]), "desktop")
        plan, state = _merge(local, peer)
        self.assertEqual([item.kind for item in plan.items], [MergeKind.ADD])
        self.assertEqual(state["local-projects"]["d1"], _project("d1", "Theirs", "D:\\P\\theirs"))
        self.assertIn("l1", state["local-projects"])  # nothing removed
        self.assertEqual(state["electron-main-window-bounds"], {"x": 1})  # untouched
        self.assertIn(
            validate_global_state_references(_bytes(state)).status,
            {ValidationStatus.PASS, ValidationStatus.PASS_WITH_WARNING},
        )

    def test_the_same_folder_under_another_id_is_matched_not_duplicated(self) -> None:
        local = _state([_project("l1", "Proj", "d:/p/proj/")], bindings={})
        peer = publication_from_state(
            _state([_project("d1", "Proj", "D:\\P\\Proj")], pinned=["d1"], bindings={"t9": "d1"}), "desktop",
        )
        plan, state = _merge(local, peer)
        self.assertEqual(plan.items[0].kind, MergeKind.MATCHED)
        self.assertEqual(plan.items[0].local_project_id, "l1")
        self.assertEqual(set(state["local-projects"]), {"l1"})
        self.assertEqual(state["pinned-project-ids"], ["l1"])
        self.assertEqual(state["thread-project-assignments"]["t9"], {"projectKind": "local", "projectId": "l1"})

    def test_peer_pins_and_order_win_for_shared_projects_and_local_ones_keep_theirs(self) -> None:
        local = _state(
            [_project("a", "A", "D:\\a"), _project("b", "B", "D:\\b"), _project("own", "Own", "D:\\own")],
            order=["own", "a", "b"], pinned=["b", "own"],
        )
        peer = publication_from_state(
            _state([_project("a", "A", "D:\\a"), _project("b", "B", "D:\\b")], order=["b", "a"], pinned=["a"]),
            "desktop",
        )
        plan, state = _merge(local, peer)
        self.assertEqual(state["project-order"], ["b", "a", "own"])
        self.assertEqual(state["pinned-project-ids"], ["a", "own"])
        self.assertTrue(plan.pins_changed and plan.order_changed)

    def test_a_missing_folder_is_added_and_said(self) -> None:
        peer = publication_from_state(_state([_project("d1", "Gone", "D:\\nowhere")]), "desktop")
        plan, state = _merge(_state([]), peer, folders=lambda root: False)
        self.assertEqual(plan.items[0].kind, MergeKind.ADD)
        self.assertEqual(plan.items[0].codes, (FOLDER_MISSING_HERE,))
        self.assertIn("d1", state["local-projects"])

    def test_a_mapped_root_is_written_as_it_reads_here(self) -> None:
        peer = publication_from_state(_state([_project("d1", "P", "D:\\Work\\p")]), "desktop")
        plan, state = _merge(_state([]), peer, map_root=lambda peer, root: root.replace("D:\\Work", "E:\\W"))
        self.assertEqual(state["local-projects"]["d1"]["rootPaths"], ["E:\\W\\p"])

    def test_two_local_candidates_or_an_ambiguous_mapping_leave_the_project_alone(self) -> None:
        local = _state([_project("x", "X", "D:\\p"), _project("y", "Y", "D:\\p")])
        peer = publication_from_state(_state([_project("d1", "P", "D:\\p")]), "desktop")
        plan, state = _merge(local, peer)
        self.assertEqual(plan.items[0].codes, (SEVERAL_LOCAL_CANDIDATES,))
        self.assertEqual(set(state["local-projects"]), {"x", "y"})

        def ambiguous(peer, root):
            raise RootMappingAmbiguous("AMBIGUOUS_MAPPING")

        plan, _ = _merge(_state([]), peer, map_root=ambiguous)
        self.assertEqual(plan.items[0].kind, MergeKind.AMBIGUOUS)
        self.assertFalse(plan.writes)

    def test_same_id_with_another_root_keeps_this_machines_root(self) -> None:
        local = _state([_project("a", "A", "D:\\here")])
        peer = publication_from_state(_state([_project("a", "A", "D:\\there")]), "desktop")
        plan, state = _merge(local, peer)
        self.assertEqual(plan.items[0].codes, (ROOT_DIFFERS,))
        self.assertEqual(state["local-projects"]["a"]["rootPaths"], ["D:\\here"])

    def test_merging_what_is_already_here_changes_nothing(self) -> None:
        state = _state([_project("a", "A", "D:\\a")], pinned=["a"], bindings={"t": "a"})
        plan, _ = _merge(state, publication_from_state(state, "desktop"))
        self.assertFalse(plan.writes)

    def test_the_plan_id_follows_the_state_bytes(self) -> None:
        peer = publication_from_state(_state([_project("d1", "P", "D:\\p")]), "desktop")
        first, _ = _merge(_state([]), peer)
        second, _ = _merge(_state([_project("z", "Z", "D:\\z")]), peer)
        self.assertNotEqual(first.plan_id, second.plan_id)

    def test_root_key_compares_windows_paths_loosely_and_posix_paths_exactly(self) -> None:
        self.assertEqual(root_key("D:/P/x/"), root_key("d:\\p\\X"))
        self.assertEqual(root_key("\\\\?\\D:\\p"), root_key("D:\\p"))
        self.assertNotEqual(root_key("/home/a"), root_key("/home/A"))


class TwoMachineTests(_Workspace):
    """The scenario from 2026-10-01: desktop's projects must show on the laptop."""

    def codex_state(self, local: Path, state: dict) -> Path:
        path = local / ".codex-global-state.json"
        path.write_bytes(_bytes(state))
        return path

    def read_state(self, local: Path) -> dict:
        return json.loads((local / ".codex-global-state.json").read_text(encoding="utf-8"))

    def test_desktop_projects_reach_the_laptop_and_the_laptops_go_back(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        self.codex_state(desktop_codex, _state(
            [_project("lab", "LabTakt", str(self.root / "LabTakt")),
             _project("chl", "project-chloya", str(self.root / "chloya"))],
            pinned=["lab", "chl"], bindings={"chat-1": "lab"},
        ))
        self.codex_state(laptop_codex, _state([_project("own", "NetRuleRouter", str(self.root / "nrr"))]))

        run_handoff(desktop)
        run_handoff(laptop)
        on_laptop = self.read_state(laptop_codex)
        self.assertEqual(set(on_laptop["local-projects"]), {"lab", "chl", "own"})
        self.assertEqual(on_laptop["pinned-project-ids"], ["lab", "chl"])
        self.assertEqual(on_laptop["project-order"][:2], ["lab", "chl"])
        self.assertEqual(
            on_laptop["thread-project-assignments"]["chat-1"], {"projectKind": "local", "projectId": "lab"},
        )
        self.assertEqual(on_laptop["electron-main-window-bounds"], {"x": 1})

        run_handoff(desktop)
        self.assertIn("own", self.read_state(desktop_codex)["local-projects"])

        # Settled: another round changes nothing on either side.
        self.assertFalse(sync_projects(laptop).plan.writes)
        self.assertFalse(sync_projects(desktop).plan.writes)

        # The history says what the run did, not only that it ran.
        from codexsync.recovery import list_history

        (run,) = list_history(laptop, family="project-sync")[-1:]
        self.assertEqual(run.counts["projects_added"], 2)
        self.assertEqual(run.origin, "handoff")

    def test_a_list_already_taken_is_not_reapplied_over_a_local_change(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        self.codex_state(desktop_codex, _state([_project("a", "A", "D:\\a")], pinned=["a"]))
        self.codex_state(laptop_codex, _state([]))
        run_handoff(desktop)
        run_handoff(laptop)
        state = self.read_state(laptop_codex)
        state["pinned-project-ids"] = []  # unpinned on the laptop afterwards
        self.codex_state(laptop_codex, state)
        self.assertEqual(sync_projects(laptop).pending, ())
        self.assertFalse(sync_projects(laptop).plan.writes)

    def test_apply_needs_codex_closed_and_the_previewed_id(self) -> None:
        desktop, desktop_codex = self.machine("desktop")
        laptop, laptop_codex = self.machine("laptop")
        self.codex_state(desktop_codex, _state([_project("a", "A", "D:\\a")]))
        self.codex_state(laptop_codex, _state([]))
        run_handoff(desktop)
        preview = sync_projects(laptop)
        self.assertEqual(len(preview.plan.added), 1)
        with self.assertRaises(ConfigError):
            sync_projects(laptop, confirm_plan="not-the-id")
        self.gate.state = ProcessState.RUNNING
        with self.assertRaises(SafetyPreconditionError):
            sync_projects(laptop, confirm_plan=preview.plan.plan_id)
        self.assertEqual(self.read_state(laptop_codex)["local-projects"], {})
        self.gate.state = ProcessState.STOPPED
        done = sync_projects(laptop, confirm_plan=preview.plan.plan_id)
        self.assertEqual(done.written, 2)  # the project, and its place in the order
        backups = list((self.workspace / "backups").rglob(".codex-global-state.json"))
        self.assertTrue(backups, "the replaced state must be backed up first")

    def test_a_machine_without_a_global_state_still_hands_off(self) -> None:
        laptop, _ = self.machine("laptop")
        with self.assertRaises(ProjectsNotCarried):
            sync_projects(laptop)
        self.assertEqual(run_handoff(laptop).projects_added, 0)


if __name__ == "__main__":
    unittest.main()
