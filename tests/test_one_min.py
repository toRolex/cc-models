"""Default [1m] output policy and per-model exclusions (core unit).

Raw IDs keep their identity everywhere in the local layer; the [1m] suffix
only exists at the settings output boundary via Selection.output_model_id.
"""

from __future__ import annotations

import json

import pytest
from test_core import BASE_SETTINGS, SECRET_TOKEN, write_settings

from cc_models.domain import (
    Catalog,
    DefaultEdit,
    PickerOption,
    Selection,
    SelectionError,
)
from cc_models.settings import (
    apply_selection,
    build_model_picker,
    compute_new_state,
    import_selection_from_settings,
    preview_apply,
    read_snapshot,
)
from cc_models.store import load_state, save_selection


def make_selection(exclusions=()):
    return Selection(
        options=[
            PickerOption("claude-opus-4-6", "Opus"),
            PickerOption("claude-sonnet-4-6", "Sonnet", description="balanced"),
            PickerOption("native-model[1m]", "Native", description="native suffix"),
        ],
        one_min_exclusions=frozenset(exclusions),
    )


def test_output_model_id_rules():
    selection = make_selection(exclusions=("claude-sonnet-4-6",))
    assert selection.output_model_id("claude-sonnet-4-6") == "claude-sonnet-4-6"
    assert selection.output_model_id("claude-opus-4-6") == "claude-opus-4-6[1m]"
    assert selection.output_model_id("native-model[1m]") == "native-model[1m]"
    plain = Selection()
    assert plain.output_model_id("Big[1M]") == "Big[1M]"
    assert plain.output_model_id("plain") == "plain[1m]"


def test_missing_one_min_field_defaults_to_empty():
    legacy = {
        "default_edit": {"kind": "preserve", "value": None},
        "options": [{"model": "m1", "label": "One"}],
    }
    selection = Selection.from_json(legacy)
    assert selection.one_min_exclusions == frozenset()


def test_selection_json_roundtrip_stable_output():
    selection = make_selection(exclusions=("claude-sonnet-4-6",))
    payload = selection.to_json()
    assert payload["one_min_exclusions"] == ["claude-sonnet-4-6"]
    restored = Selection.from_json(json.loads(json.dumps(payload)))
    assert restored.one_min_exclusions == frozenset({"claude-sonnet-4-6"})
    assert restored.ids == selection.ids


def test_exclusions_roundtrip_via_store(tmp_path):
    selection = make_selection(exclusions=("claude-sonnet-4-6",))
    save_selection(tmp_path, selection)
    state = load_state(tmp_path)
    assert state.selection.one_min_exclusions == frozenset({"claude-sonnet-4-6"})
    assert state.selection.ids == selection.ids


def test_invalid_exclusions_refused():
    with pytest.raises(SelectionError, match="not a selected raw ID"):
        make_selection(exclusions=("not-selected",))
    with pytest.raises(SelectionError, match="one_min_exclusions"):
        Selection(
            options=[PickerOption("m1", "One")],
            one_min_exclusions={"m1"},  # type: ignore[arg-type]  # set, not frozenset
        )
    with pytest.raises(SelectionError, match="nonempty strings"):
        Selection.from_json(
            {
                "default_edit": {"kind": "preserve", "value": None},
                "options": [{"model": "m1", "label": "One"}],
                "one_min_exclusions": ["m1", ""],
            }
        )
    with pytest.raises(SelectionError, match="unique"):
        Selection.from_json(
            {
                "default_edit": {"kind": "preserve", "value": None},
                "options": [{"model": "m1", "label": "One"}],
                "one_min_exclusions": ["m1", "m1"],
            }
        )
    with pytest.raises(SelectionError, match="must be a list"):
        Selection.from_json(
            {
                "default_edit": {"kind": "preserve", "value": None},
                "options": [{"model": "m1", "label": "One"}],
                "one_min_exclusions": "m1",
            }
        )


def test_build_model_picker_transforms_output_and_preserves_raw():
    selection = make_selection(exclusions=("claude-sonnet-4-6",))
    picker = build_model_picker(selection)
    assert [opt["model"] for opt in picker["options"]] == [
        "claude-opus-4-6[1m]",
        "claude-sonnet-4-6",
        "native-model[1m]",
    ]
    assert selection.ids == ["claude-opus-4-6", "claude-sonnet-4-6", "native-model[1m]"]


def test_explicit_default_set_goes_through_output_rule(tmp_path):
    path = write_settings(tmp_path, BASE_SETTINGS)
    snapshot = read_snapshot(path)
    selection = make_selection(exclusions=("claude-sonnet-4-6",))
    selection.default_edit = DefaultEdit("set", "claude-sonnet-4-6")
    new_data, _, _ = compute_new_state(snapshot, selection)
    assert new_data["model"] == "claude-sonnet-4-6"  # excluded: written raw

    selection.default_edit = DefaultEdit("set", "claude-opus-4-6")
    new_data, _, _ = compute_new_state(snapshot, selection)
    assert new_data["model"] == "claude-opus-4-6[1m]"


def test_preserved_default_never_touched(tmp_path):
    data = {**BASE_SETTINGS, "model": "claude-haiku-4-5"}
    path = write_settings(tmp_path, data)
    snapshot = read_snapshot(path)
    selection = make_selection()
    new_data, warnings, changes = compute_new_state(snapshot, selection)
    assert new_data["model"] == "claude-haiku-4-5"
    assert "model" not in changes
    assert "preserved default model not in selection: claude-haiku-4-5" in warnings


def test_repeated_apply_is_noop_and_case_suffix_idempotent(tmp_path):
    path = write_settings(tmp_path, BASE_SETTINGS)
    snapshot = read_snapshot(path)
    first = apply_selection(snapshot, make_selection(), tmp_path)
    assert first.applied is True

    second = apply_selection(read_snapshot(path), make_selection(), tmp_path)
    assert second.no_op is True
    assert second.applied is False

    written = json.dumps(read_snapshot(path).data)
    assert "[1m][1m]" not in written


def test_output_collision_refused(tmp_path):
    path = write_settings(tmp_path, BASE_SETTINGS)
    snapshot = read_snapshot(path)
    selection = Selection(
        options=[
            PickerOption("m1", "One"),
            PickerOption("m1[1m]", "One suffixed"),
        ]
    )
    with pytest.raises(SelectionError, match="collision"):
        compute_new_state(snapshot, selection)
    # excluding the un-suffixed raw ID resolves the collision
    fixed = Selection(
        options=[
            PickerOption("m1", "One"),
            PickerOption("m1[1m]", "One suffixed"),
        ],
        one_min_exclusions=frozenset({"m1"}),
    )
    _, _, changes = compute_new_state(snapshot, fixed)
    assert changes["modelPicker"]["after"]["options"][0]["model"] == "m1"


def test_empty_selection_refused(tmp_path):
    path = write_settings(tmp_path, BASE_SETTINGS)
    snapshot = read_snapshot(path)
    with pytest.raises(SelectionError, match="empty selection"):
        preview_apply(snapshot, Selection())


def test_raw_catalog_membership_ignores_output_suffix():
    catalog = Catalog(
        endpoint="http://127.0.0.1:2317/v1",
        fetched_at="2026-01-01T00:00:00+00:00",
        model_ids=["m1", "native-model[1m]"],
    )
    selection = make_selection(exclusions=("claude-sonnet-4-6",))
    assert catalog.missing_from(selection) == ["claude-opus-4-6", "claude-sonnet-4-6"]


def _settings_with_secret(tmp_path, options):
    data = dict(BASE_SETTINGS)
    data["modelPicker"] = {"replaceBuiltInOptions": False, "options": options}
    return read_snapshot(write_settings(tmp_path, data))


def test_unknown_fields_exact_raw_then_alias_fallback(tmp_path):
    snapshot = _settings_with_secret(
        tmp_path,
        [
            {"model": "m1", "label": "One", "api_key": SECRET_TOKEN},
        ],
    )
    selection = Selection(options=[PickerOption("m1", "One")])
    new_data, _, _ = compute_new_state(snapshot, selection)
    assert new_data["modelPicker"]["options"][0]["api_key"] == SECRET_TOKEN
    assert new_data["modelPicker"]["options"][0]["model"] == "m1[1m]"

    applied_path = write_settings(tmp_path, new_data, name="settings2.json")
    second_snapshot = read_snapshot(applied_path)
    new_data2, _, _ = compute_new_state(second_snapshot, selection)
    assert new_data2["modelPicker"]["options"][0]["api_key"] == SECRET_TOKEN


def test_unknown_fields_survive_opt_out_toggle(tmp_path):
    snapshot = _settings_with_secret(
        tmp_path,
        [{"model": "m1", "label": "One", "api_key": SECRET_TOKEN}],
    )
    selection = Selection(options=[PickerOption("m1", "One")])
    new_data, _, _ = compute_new_state(snapshot, selection)
    opted = Selection(
        options=[PickerOption("m1", "One")],
        one_min_exclusions=frozenset({"m1"}),
    )
    applied_path = write_settings(tmp_path, new_data, name="settings3.json")
    new_data2, _, _ = compute_new_state(read_snapshot(applied_path), opted)
    assert new_data2["modelPicker"]["options"][0]["model"] == "m1"
    assert new_data2["modelPicker"]["options"][0]["api_key"] == SECRET_TOKEN


def test_exact_raw_priority_and_alias_competition(tmp_path):
    snapshot = _settings_with_secret(
        tmp_path,
        [
            {"model": "m1", "label": "Raw", "api_key": "raw-key"},
            {"model": "m1[1m]", "label": "Suffixed", "api_key": "suffixed-key"},
        ],
    )
    selection = Selection(
        options=[PickerOption("m1", "One")],
        one_min_exclusions=frozenset({"m1"}),
    )
    new_data, _, _ = compute_new_state(snapshot, selection)
    assert new_data["modelPicker"]["options"][0]["api_key"] == "raw-key"


def test_alias_source_owned_by_selected_literal_is_skipped_with_warning(tmp_path):
    snapshot = _settings_with_secret(
        tmp_path,
        [{"model": "m1[1m]", "label": "Suffixed", "api_key": "suffixed-key"}],
    )
    selection = Selection(
        options=[PickerOption("m1", "One"), PickerOption("m1[1m]", "Suffixed")],
        one_min_exclusions=frozenset({"m1"}),
    )
    new_data, warnings, _ = compute_new_state(snapshot, selection)
    raw_opt = next(o for o in new_data["modelPicker"]["options"] if o["model"] == "m1")
    assert "api_key" not in raw_opt
    assert any("belongs to another selected option" in w for w in warnings)
    # the literal suffixed option still matches its own source exactly
    lit_opt = next(o for o in new_data["modelPicker"]["options"] if o["model"] == "m1[1m]")
    assert lit_opt["api_key"] == "suffixed-key"


def test_ambiguous_alias_candidates_raise_before_write(tmp_path):
    snapshot = _settings_with_secret(
        tmp_path,
        [
            {"model": "m1[1m]", "label": "A", "api_key": "a"},
            {"model": "m1[1M]", "label": "B", "api_key": "b"},
        ],
    )
    selection = Selection(options=[PickerOption("m1", "One")])
    with pytest.raises(SelectionError, match="ambiguous"):
        compute_new_state(snapshot, selection)


def test_ambiguous_exact_candidates_raise_before_write(tmp_path):
    snapshot = _settings_with_secret(
        tmp_path,
        [
            {"model": "m1", "label": "A", "api_key": "a"},
            {"model": "m1", "label": "B", "api_key": "b"},
        ],
    )
    selection = Selection(options=[PickerOption("m1", "One")])
    with pytest.raises(SelectionError, match="ambiguous"):
        compute_new_state(snapshot, selection)


def test_unknown_fields_never_leak_via_preview(tmp_path):
    snapshot = _settings_with_secret(
        tmp_path,
        [{"model": "m1", "label": "One", "api_key": SECRET_TOKEN}],
    )
    selection = Selection(options=[PickerOption("m1", "One")])
    preview = preview_apply(snapshot, selection)
    assert SECRET_TOKEN not in json.dumps(preview.managed_changes)
    after_options = preview.managed_changes["modelPicker"]["after"]["options"]
    assert after_options[0]["model"] == "m1[1m]"
    assert after_options[0]["api_key"] == "<unknown field redacted>"


def test_import_keeps_literal_suffixed_ids(tmp_path):
    data = dict(BASE_SETTINGS)
    data["modelPicker"] = {
        "options": [{"model": "native[1m]", "label": "Native"}],
    }
    snapshot = read_snapshot(write_settings(tmp_path, data))
    selection, _ = import_selection_from_settings(snapshot)
    assert selection.ids == ["native[1m]"]
    assert selection.one_min_exclusions == frozenset()
    picker = build_model_picker(selection)
    assert picker["options"][0]["model"] == "native[1m]"


def test_rename_reorder_refresh_keep_raw_identity():
    selection = make_selection(exclusions=("claude-sonnet-4-6",))
    reordered = Selection(
        options=list(reversed(selection.options)),
        one_min_exclusions=frozenset({"claude-sonnet-4-6"}),
    )
    assert reordered.ids == ["native-model[1m]", "claude-sonnet-4-6", "claude-opus-4-6"]
    assert [o.label for o in reordered.options] == ["Native", "Sonnet", "Opus"]


def test_exclusions_serialize_in_selection_order():
    selection = Selection(
        options=[PickerOption("z", "Zee"), PickerOption("a", "Aye")],
        one_min_exclusions=frozenset({"z", "a"}),
    )
    assert selection.to_json()["one_min_exclusions"] == ["z", "a"]


def test_single_wrong_case_source_is_not_copied(tmp_path):
    snapshot = _settings_with_secret(
        tmp_path,
        [{"model": "Model[1m]", "label": "Other", "api_key": SECRET_TOKEN}],
    )
    selection = Selection(options=[PickerOption("model", "Lower")])
    new_data, _, _ = compute_new_state(snapshot, selection)
    assert "api_key" not in new_data["modelPicker"]["options"][0]


def test_case_distinct_raw_options_do_not_share_source(tmp_path):
    snapshot = _settings_with_secret(
        tmp_path,
        [{"model": "Model[1m]", "label": "Upper", "api_key": "upper-key"}],
    )
    selection = Selection(options=[PickerOption("Model", "Up"), PickerOption("model", "Low")])
    new_data, warnings, _ = compute_new_state(snapshot, selection)
    opts = {o["model"]: o for o in new_data["modelPicker"]["options"]}
    assert opts["Model[1m]"]["api_key"] == "upper-key"
    assert "api_key" not in opts["model[1m]"]
    assert not any("belongs to another" in w for w in warnings)


def test_native_suffix_raw_never_double_suffix_alias(tmp_path):
    snapshot = _settings_with_secret(
        tmp_path,
        [{"model": "native[1m][1m]", "label": "Double", "api_key": "other-key"}],
    )
    selection = Selection(options=[PickerOption("native[1m]", "Native")])
    new_data, _, _ = compute_new_state(snapshot, selection)
    assert "api_key" not in new_data["modelPicker"]["options"][0]
    excluded = Selection(
        options=[PickerOption("native[1m]", "Native")],
        one_min_exclusions=frozenset({"native[1m]"}),
    )
    new_data, _, _ = compute_new_state(snapshot, excluded)
    assert "api_key" not in new_data["modelPicker"]["options"][0]


def test_preserved_default_warning_uses_emitted_ids_both_directions(tmp_path):
    path = write_settings(tmp_path, {**BASE_SETTINGS, "model": "m1[1m]"})
    snapshot = read_snapshot(path)
    _, warnings, _ = compute_new_state(snapshot, Selection(options=[PickerOption("m1", "One")]))
    assert not any("preserved default" in w for w in warnings)

    path = write_settings(tmp_path, {**BASE_SETTINGS, "model": "m1"}, name="s2.json")
    _, warnings, _ = compute_new_state(
        read_snapshot(path), Selection(options=[PickerOption("m1", "One")])
    )
    assert "preserved default model not in selection: m1" in warnings


def test_one_min_capability_warning_in_preview_and_apply(tmp_path):
    path = write_settings(tmp_path, BASE_SETTINGS)
    snapshot = read_snapshot(path)
    selection = make_selection()
    preview = preview_apply(snapshot, selection)
    assert any(
        "client window declaration" in w and "CPA/upstream compatibility is not assured" in w
        for w in preview.warnings
    )
    result = apply_selection(snapshot, selection, tmp_path)
    assert any("client window declaration" in w for w in result.warnings)
    # excluded-only selection emits no suffixed IDs and no warning
    only_raw = Selection(
        options=[PickerOption("claude-opus-4-6", "Opus")],
        one_min_exclusions=frozenset({"claude-opus-4-6"}),
    )
    _, warnings, _ = compute_new_state(read_snapshot(path), only_raw)
    assert not any("client window declaration" in w for w in warnings)
