from analyzepdf.text_evidence import choose_text_evidence_route


def test_native_view_is_recommended_only_when_character_evidence_agrees() -> None:
    agreed = choose_text_evidence_route(
        parsed_text="第五十九条 下级行政复议机关\n决定报上级备案。",
        native_text="第五十九条下级行政复议机关决定报上级备案。",
    )
    assert agreed["decision"] == "native_primary"
    assert agreed["requires_reconciliation"] is False

    conflict = choose_text_evidence_route(
        parsed_text="第五十九条完全不同的内容",
        native_text="第五十九条下级行政复议机关决定报上级备案。",
    )
    assert conflict["decision"] == "unresolved"
    assert conflict["requires_reconciliation"] is True
