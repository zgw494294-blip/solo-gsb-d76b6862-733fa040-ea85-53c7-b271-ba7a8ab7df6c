"""POST /v1/recall/location-trace 的端到端测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

FNC1 = "\x1d"  # ASCII 29


def make_gtin(body13: str) -> str:
    """由 13 位本体计算模 10 校验位,生成合法 14 位 GTIN。"""
    assert len(body13) == 13 and body13.isdigit()
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body13)))
    return body13 + str((10 - total % 10) % 10)


GTIN = make_gtin("0950600112345")
GTIN2 = make_gtin("0950600112346")
GTIN3 = make_gtin("0950600112347")
GTIN4 = make_gtin("0950600112348")


def scan(gtin=GTIN, serial="SN1", batch="LOT1", expiry="251231"):
    """按 01/17/10/21 顺序拼接扫描串(变长字段后视需要补 FNC1)。"""
    s = "01" + gtin
    if expiry is not None:
        s += "17" + expiry
    if batch is not None:
        s += "10" + batch
        if serial is not None:
            s += FNC1
    if serial is not None:
        s += "21" + serial
    return s


def rule(rid, gtin=GTIN, **kw):
    return {"id": rid, "gtin": gtin, **kw}


def transfer(sc, at, to_loc, from_loc=None):
    return {
        "action": "transfer",
        "scan": sc,
        "occurred_at": at,
        "from_location": from_loc,
        "to_location": to_loc,
    }


def pack(parent, child, at="2026-09-01T00:00:00Z"):
    return {"action": "pack", "parent_scan": parent, "child_scan": child, "occurred_at": at}


def unpack(parent, child, at="2026-09-01T00:00:00Z"):
    return {"action": "unpack", "parent_scan": parent, "child_scan": child, "occurred_at": at}


def seal(sc, sid, at):
    return {"action": "seal", "scan": sc, "seal_id": sid, "occurred_at": at}


def unseal(sc, sid, at):
    return {"action": "unseal", "scan": sc, "seal_id": sid, "occurred_at": at}


def post(scans, rules, events, expected=200, contact_until=None):
    payload: dict = {"scans": scans, "rules": rules, "events": events}
    if contact_until is not None:
        payload["contact_until"] = contact_until
    resp = client.post("/v1/recall/location-trace", json=payload)
    assert resp.status_code == expected, resp.text
    return resp.json()


PALLET = scan(GTIN, "PALLET")
CASE = scan(GTIN2, "CASE")
ITEM = scan(GTIN3, "ITEM")
ALL_AT_A = [
    transfer(PALLET, "2026-09-01T00:00:00Z", "WH-A"),
    transfer(CASE, "2026-09-01T00:01:00Z", "WH-A"),
    transfer(ITEM, "2026-09-01T00:02:00Z", "WH-A"),
]


# ---------- 库存扫描 422 ----------


def test_inventory_invalid_scan_returns_422():
    resp = client.post(
        "/v1/recall/location-trace",
        json={
            "scans": ["01" + GTIN + "99"],
            "rules": [rule("R1")],
            "events": [transfer(scan(), "2026-09-01T00:00:00Z", "WH-A")],
        },
    )
    assert resp.status_code == 422
    assert "库存扫描 0 非法" in resp.json()["detail"]


def test_inventory_missing_serial_returns_422():
    resp = client.post(
        "/v1/recall/location-trace",
        json={
            "scans": ["01" + GTIN + "1725123110LOT1"],
            "rules": [rule("R1")],
            "events": [transfer(scan(), "2026-09-01T00:00:00Z", "WH-A")],
        },
    )
    assert resp.status_code == 422
    assert "缺少 AI 21" in resp.json()["detail"]


def test_inventory_duplicate_identity_returns_422():
    resp = client.post(
        "/v1/recall/location-trace",
        json={
            "scans": [scan(GTIN, "A", batch="L1"), scan(GTIN, "A", batch="L2")],
            "rules": [rule("R1")],
            "events": [transfer(scan(GTIN, "A"), "2026-09-01T00:00:00Z", "WH-A")],
        },
    )
    assert resp.status_code == 422
    assert "重复" in resp.json()["detail"]


def test_validation_counts_fields_and_rules():
    good_event = transfer(scan(), "2026-09-01T00:00:00Z", "WH-A")

    def payload(scans, rules, events):
        return {"scans": scans, "rules": rules, "events": events}

    _expect_422(payload([], [rule("R1")], [good_event]))  # 空库存
    _expect_422(payload([scan()] * 101, [rule("R1")], [good_event]))
    _expect_422(payload([scan()], [], [good_event]))
    _expect_422(payload([scan()], [rule("R1")], []))
    _expect_422(payload([scan()], [rule("R1")], [good_event] * 201))
    _expect_422(payload([scan()], [rule("R1"), rule("R1")], [good_event]))  # 规则 ID 重复
    _expect_422(
        payload([scan()], [rule("R1", gtin="09506001123450")], [good_event])
    )  # GTIN 校验位
    # 边界:100 条库存、200 条事件合法
    scans100 = [scan(GTIN, f"SN{i}") for i in range(100)]
    body = post(
        scans100,
        [rule("R1")],
        [transfer(scans100[0], "2026-09-01T00:00:00Z", "WH-A")] * 200,
    )
    # 首次之后的交接 from_location 不为 null,全部报错,但请求合法
    assert len(body["errors"]) == 199


def test_validation_event_shapes():
    _expect_422(
        {
            "scans": [scan()],
            "rules": [rule("R1")],
            "events": [{"action": "move", "occurred_at": "2026-09-01T00:00:00Z"}],
        }
    )
    _expect_422(
        {
            "scans": [scan()],
            "rules": [rule("R1")],
            "events": [
                {
                    "action": "transfer",
                    "scan": scan(),
                    "occurred_at": "2026-09-01T00:00:00",  # 缺时区
                    "from_location": None,
                    "to_location": "A",
                }
            ],
        }
    )
    _expect_422(
        {
            "scans": [scan()],
            "rules": [rule("R1")],
            "events": [
                {
                    "action": "transfer",
                    "scan": scan(),
                    "occurred_at": "2026-09-01T00:00:00Z",
                    "from_location": None,
                    "to_location": "",  # 空接收方
                }
            ],
        }
    )
    _expect_422(
        {
            "scans": [scan()],
            "rules": [rule("R1")],
            "events": [
                {  # transfer 缺 to_location
                    "action": "transfer",
                    "scan": scan(),
                    "occurred_at": "2026-09-01T00:00:00Z",
                    "from_location": None,
                }
            ],
        }
    )
    _expect_422(
        {
            "scans": [scan()],
            "rules": [rule("R1")],
            "events": [
                {  # pack 多余字段
                    "action": "pack",
                    "parent_scan": scan(),
                    "child_scan": scan(GTIN2, "C"),
                    "occurred_at": "2026-09-01T00:00:00Z",
                    "to_location": "A",
                }
            ],
        }
    )
    _expect_422(
        {
            "scans": [scan()],
            "rules": [rule("R1")],
            "events": [
                {  # 顶层多余字段
                    "scans": [scan()],
                    "extra": 1,
                }
            ],
        }
    )


def _expect_422(payload):
    resp = client.post("/v1/recall/location-trace", json=payload)
    assert resp.status_code == 422, resp.text


# ---------- 基本交接链与地点去重 ----------


def test_simple_transfer_chain_locations():
    events = [
        transfer(scan(), "2026-09-01T02:00:00Z", "WH-C", from_loc="WH-B"),
        transfer(scan(), "2026-09-01T00:00:00Z", "WH-A"),
        transfer(scan(), "2026-09-01T01:00:00Z", "WH-B", from_loc="WH-A"),
    ]
    body = post([scan()], [rule("R1")], events)
    assert body["errors"] == []
    assert body["items"] == [
        {"gtin": GTIN, "serial": "SN1", "locations": ["WH-A", "WH-B", "WH-C"], "rule_ids": ["R1"]}
    ]


def test_locations_deduped_by_first_arrival():
    events = [
        transfer(scan(), "2026-09-01T00:00:00Z", "WH-A"),
        transfer(scan(), "2026-09-01T01:00:00Z", "WH-B", from_loc="WH-A"),
        transfer(scan(), "2026-09-01T02:00:00Z", "WH-A", from_loc="WH-B"),
        transfer(scan(), "2026-09-01T03:00:00Z", "WH-B", from_loc="WH-A"),
    ]
    body = post([scan()], [rule("R1")], events)
    assert body["items"][0]["locations"] == ["WH-A", "WH-B"]


def test_first_transfer_from_must_be_null():
    events = [
        transfer(scan(), "2026-09-01T00:00:00Z", "WH-A", from_loc="WH-X"),  # 失败
        transfer(scan(), "2026-09-01T01:00:00Z", "WH-B"),  # 仍是首次,成功
    ]
    body = post([scan()], [rule("R1")], events)
    assert body["items"][0]["locations"] == ["WH-B"]
    assert body["errors"][0]["index"] == 0
    assert "null" in body["errors"][0]["reason"]


def test_broken_chain_failed_event_has_no_effect():
    events = [
        transfer(scan(), "2026-09-01T00:00:00Z", "WH-A"),
        transfer(scan(), "2026-09-01T01:00:00Z", "WH-B", from_loc="WH-X"),  # 链断裂
        transfer(scan(), "2026-09-01T02:00:00Z", "WH-B", from_loc="WH-A"),  # 以 A 为当前持有人,成功
    ]
    body = post([scan()], [rule("R1")], events)
    assert body["items"][0]["locations"] == ["WH-A", "WH-B"]
    assert [e["index"] for e in body["errors"]] == [1]


def test_utc_order_and_same_instant_input_order():
    events = [
        transfer(scan(), "2026-09-01T08:00:00+08:00", "WH-A"),  # UTC 00:00
        transfer(scan(), "2026-09-01T00:00:00Z", "WH-B", from_loc="WH-A"),  # 同刻,依输入顺序
    ]
    body = post([scan()], [rule("R1")], events)
    assert body["errors"] == []
    assert body["items"][0]["locations"] == ["WH-A", "WH-B"]
    # 反序后第二条的 from 不再成立
    body = post([scan()], [rule("R1")], list(reversed(events)))
    assert [e["index"] for e in body["errors"]] == [0]
    assert body["items"][0]["locations"] == ["WH-A"]


# ---------- 容器交接与后代传播 ----------


def test_container_transfer_moves_all_descendants():
    events = [
        *ALL_AT_A,
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        pack(CASE, ITEM, "2026-09-01T02:00:00Z"),
        transfer(PALLET, "2026-09-01T03:00:00Z", "WH-B", from_loc="WH-A"),
    ]
    body = post([PALLET, CASE, ITEM], [rule("R1", gtin=GTIN3)], events)
    assert body["errors"] == []
    (item,) = body["items"]
    assert (item["gtin"], item["serial"]) == (GTIN3, "ITEM")
    # ITEM 自身先到 WH-A,随容器交接再到 WH-B
    assert item["locations"] == ["WH-A", "WH-B"]


def test_descendant_locations_dedup_across_container_transfers():
    events = [
        *ALL_AT_A,
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        pack(CASE, ITEM, "2026-09-01T02:00:00Z"),
        transfer(PALLET, "2026-09-01T03:00:00Z", "WH-B", from_loc="WH-A"),
        transfer(PALLET, "2026-09-01T04:00:00Z", "WH-A", from_loc="WH-B"),  # 回到 WH-A,不重复
        transfer(PALLET, "2026-09-01T05:00:00Z", "WH-C", from_loc="WH-A"),
    ]
    body = post([PALLET, CASE, ITEM], [rule("R1", gtin=GTIN3)], events)
    assert body["items"][0]["locations"] == ["WH-A", "WH-B", "WH-C"]


def test_transfer_with_parent_rejected():
    events = [
        *ALL_AT_A,
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        transfer(CASE, "2026-09-01T02:00:00Z", "WH-X", from_loc="WH-A"),  # CASE 已有父件
    ]
    body = post([PALLET, CASE, ITEM], [rule("R1", gtin=GTIN2)], events)
    assert [e["index"] for e in body["errors"]] == [4]
    assert "仅无父件实物可直接交接" in body["errors"][0]["reason"]
    # 失败不改变持有人
    assert body["items"][0]["locations"] == ["WH-A"]


def test_unpack_keeps_current_holder_and_no_duplicate_location():
    events = [
        *ALL_AT_A,
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        pack(CASE, ITEM, "2026-09-01T02:00:00Z"),
        transfer(PALLET, "2026-09-01T03:00:00Z", "WH-B", from_loc="WH-A"),
        unpack(CASE, ITEM, "2026-09-01T04:00:00Z"),  # 取出:保留 WH-B,不新增地点
        transfer(ITEM, "2026-09-01T05:00:00Z", "WH-C", from_loc="WH-B"),
    ]
    body = post([PALLET, CASE, ITEM], [rule("R1", gtin=GTIN3)], events)
    assert body["errors"] == []
    assert body["items"][0]["locations"] == ["WH-A", "WH-B", "WH-C"]


def test_unpack_only_removes_direct_edge():
    # PALLET->CASE->ITEM:只能取 CASE 出 PALLET,不能跨级取 ITEM
    events = [
        *ALL_AT_A,
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        pack(CASE, ITEM, "2026-09-01T02:00:00Z"),
        unpack(PALLET, ITEM, "2026-09-01T03:00:00Z"),  # 跨级,失败
    ]
    body = post([PALLET, CASE, ITEM], [rule("R1", gtin=GTIN3)], events)
    assert [e["index"] for e in body["errors"]] == [5]
    # ITEM 仍在 CASE 下;容器交接仍带着 ITEM
    events2 = events + [transfer(PALLET, "2026-09-01T04:00:00Z", "WH-B", from_loc="WH-A")]
    body2 = post([PALLET, CASE, ITEM], [rule("R1", gtin=GTIN3)], events2)
    assert body2["errors"] and body2["items"][0]["locations"] == ["WH-A", "WH-B"]


# ---------- 装入约束 ----------


def test_pack_requires_same_holder():
    item_b = transfer(ITEM, "2026-09-01T00:00:00Z", "WH-B")
    pallet_a = transfer(PALLET, "2026-09-01T00:00:00Z", "WH-A")
    events = [item_b, pallet_a, pack(PALLET, ITEM, "2026-09-01T01:00:00Z")]
    body = post([PALLET, ITEM], [rule("R1", gtin=GTIN3)], events)
    assert [e["index"] for e in body["errors"]] == [2]
    assert "持有人须相同" in body["errors"][0]["reason"]


def test_pack_requires_existing_holder():
    # 双方都在库存中但从未交接,持有人缺失
    events = [pack(PALLET, CASE, "2026-09-01T01:00:00Z")]
    body = post([PALLET, CASE], [rule("R1", gtin=GTIN2)], events)
    assert [e["index"] for e in body["errors"]] == [0]
    assert "尚无成功交接" in body["errors"][0]["reason"]


def test_pack_child_already_parented_rejected():
    pallet2 = scan(GTIN4, "PALLET2")
    events = [
        transfer(pallet2, "2026-09-01T00:00:00Z", "WH-A"),
        *ALL_AT_A,
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        pack(pallet2, CASE, "2026-09-01T02:00:00Z"),  # CASE 已有父件
    ]
    body = post([PALLET, pallet2, CASE, ITEM], [rule("R1", gtin=GTIN2)], events)
    assert [e["index"] for e in body["errors"]] == [5]
    assert "已有直接父件" in body["errors"][0]["reason"]


def test_pack_cycle_rejected():
    solo = scan(GTIN4, "SOLO")
    events = [
        transfer(solo, "2026-09-01T00:00:00Z", "WH-A"),
        *ALL_AT_A,
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        pack(CASE, ITEM, "2026-09-01T02:00:00Z"),
        pack(ITEM, PALLET, "2026-09-01T03:00:00Z"),  # PALLET 是 ITEM 的祖先,成环
        pack(solo, solo, "2026-09-01T03:30:00Z"),  # 自装
    ]
    body = post([PALLET, CASE, ITEM, solo], [rule("R1", gtin=GTIN)], events)
    assert [e["index"] for e in body["errors"]] == [6, 7]
    assert "循环包装" in body["errors"][0]["reason"]
    assert "同一实物" in body["errors"][1]["reason"]
    # 失败装入不改变关系:PALLET 仍可整树交接
    events2 = events + [transfer(PALLET, "2026-09-01T04:00:00Z", "WH-B", from_loc="WH-A")]
    body2 = post([PALLET, CASE, ITEM, solo], [rule("R1", gtin=GTIN3)], events2)
    assert body2["items"][0]["locations"] == ["WH-A", "WH-B"]


# ---------- 事件扫描校验(按原下标报错且不生效) ----------


def test_event_scan_invalid_and_missing_serial():
    events = [
        {"action": "transfer", "scan": "01" + GTIN + "99",
         "occurred_at": "2026-09-01T00:00:00Z", "from_location": None, "to_location": "WH-A"},
        {"action": "transfer", "scan": "01" + GTIN + "17251231",
         "occurred_at": "2026-09-01T01:00:00Z", "from_location": None, "to_location": "WH-B"},
        transfer(scan(), "2026-09-01T02:00:00Z", "WH-C"),
    ]
    body = post([scan()], [rule("R1")], events)
    assert [e["index"] for e in body["errors"]] == [0, 1]
    assert "扫描非法" in body["errors"][0]["reason"]
    assert "缺少 AI 21" in body["errors"][1]["reason"]
    assert body["items"][0]["locations"] == ["WH-C"]


def test_event_party_not_in_inventory():
    ghost = scan(GTIN4, "GHOST")
    events = [
        transfer(scan(), "2026-09-01T00:00:00Z", "WH-A"),
        transfer(ghost, "2026-09-01T01:00:00Z", "WH-A"),
        pack(scan(), ghost, "2026-09-01T02:00:00Z"),
    ]
    body = post([scan()], [rule("R1")], events)
    assert [e["index"] for e in body["errors"]] == [1, 2]
    assert all("不在库存扫描中" in e["reason"] for e in body["errors"])


def test_event_batch_expiry_must_match_inventory_including_absence():
    base = scan(batch="LOT1", expiry="251231")
    events = [
        transfer(base, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(scan(batch="LOT2"), "2026-09-01T01:00:00Z", "WH-B", from_loc="WH-A"),
        transfer(scan(expiry="260101"), "2026-09-01T02:00:00Z", "WH-B", from_loc="WH-A"),
        transfer(scan(batch=None), "2026-09-01T03:00:00Z", "WH-B", from_loc="WH-A"),  # 缺失冲突
        transfer(base, "2026-09-01T04:00:00Z", "WH-B", from_loc="WH-A"),  # 完全一致,成功
    ]
    body = post([base], [rule("R1")], events)
    assert [e["index"] for e in body["errors"]] == [1, 2, 3]
    assert "批号" in body["errors"][0]["reason"]
    assert "有效期" in body["errors"][1]["reason"]
    assert "批号" in body["errors"][2]["reason"]
    assert body["items"][0]["locations"] == ["WH-A", "WH-B"]


def test_inventory_without_batch_or_expiry_event_must_also_omit():
    bare = scan(batch=None, expiry=None)
    events = [
        transfer(bare, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(scan(batch="LOT1", expiry=None),
                 "2026-09-01T01:00:00Z", "WH-B", from_loc="WH-A"),  # 多了批号
        transfer(bare, "2026-09-01T02:00:00Z", "WH-B", from_loc="WH-A"),
    ]
    body = post([bare], [rule("R1")], events)
    assert [e["index"] for e in body["errors"]] == [1]
    assert body["items"][0]["locations"] == ["WH-A", "WH-B"]


def test_failed_event_does_not_change_holder_or_tree():
    events = [
        *ALL_AT_A,
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        pack(PALLET, CASE, "2026-09-01T02:00:00Z"),  # 重复装入失败,关系不变
        unpack(PALLET, CASE, "2026-09-01T03:00:00Z"),  # 正常取出成功
        transfer(PALLET, "2026-09-01T04:00:00Z", "WH-B", from_loc="WH-A"),
        # CASE 已取出,不随 PALLET 移动;其当前持有人仍为 WH-A
        transfer(CASE, "2026-09-01T05:00:00Z", "WH-C", from_loc="WH-A"),
    ]
    body = post([PALLET, CASE, ITEM], [rule("R1", gtin=GTIN2)], events)
    assert [e["index"] for e in body["errors"]] == [4]
    assert body["items"][0]["locations"] == ["WH-A", "WH-C"]


# ---------- 召回匹配与输出 ----------


def test_only_recalled_items_returned_sorted():
    a = scan(GTIN, "A")
    b = scan(GTIN, "B")
    c = scan(GTIN2, "C")
    events = [
        transfer(c, "2026-09-01T00:00:00Z", "L1"),
        transfer(b, "2026-09-01T00:00:00Z", "L2"),
        transfer(a, "2026-09-01T00:00:00Z", "L3"),
    ]
    body = post([a, b, c], [rule("R1"), rule("R2", gtin=GTIN4)], events)
    # 仅命中 GTIN 的 A、B;按 GTIN、序列号排序;未命中的 R2 不出现
    assert body["items"] == [
        {"gtin": GTIN, "serial": "A", "locations": ["L3"], "rule_ids": ["R1"]},
        {"gtin": GTIN, "serial": "B", "locations": ["L2"], "rule_ids": ["R1"]},
    ]
    assert body["errors"] == []


def test_rule_ids_in_rule_input_order_and_item_without_events():
    item = scan(GTIN3, "NEVER-MOVED")
    body = post(
        [item],
        [rule("R1", gtin=GTIN3), rule("R2", gtin=GTIN3), rule("R3", gtin=GTIN4)],
        [transfer(scan(GTIN4, "OTHER"), "2026-09-01T00:00:00Z", "WH-A")],
    )
    assert body["items"] == [
        {"gtin": GTIN3, "serial": "NEVER-MOVED", "locations": [], "rule_ids": ["R1", "R2"]}
    ]


def test_rule_fields_reused_for_matching():
    s = scan(GTIN, "A", batch="LOT1", expiry="251231")
    body = post(
        [s],
        [
            rule("R-batch", batch="LOT2"),
            rule("R-date", date_from="2026-01-01"),
            rule("R-hit", batch="LOT1", date_to="2025-12-31"),
        ],
        [transfer(s, "2026-09-01T00:00:00Z", "WH-A")],
    )
    assert body["items"][0]["rule_ids"] == ["R-hit"]


def test_event_errors_sorted_by_original_index():
    events = [
        transfer(scan(GTIN4, "X"), "2026-09-01T02:00:00Z", "WH-A"),  # 0 不在库存
        {"action": "transfer", "scan": "01" + GTIN + "99",
         "occurred_at": "2026-09-01T01:00:00Z", "from_location": None, "to_location": "A"},  # 1 非法
        transfer(scan(), "2026-09-01T00:00:00Z", "WH-A", from_loc="WH-Z"),  # 2 首次非 null
    ]
    body = post([scan()], [rule("R1")], events)
    assert [e["index"] for e in body["errors"]] == [0, 1, 2]


# ---------- contact_until:同地共处统计 ----------


HIT = scan(GTIN3, "HIT")
MISS1 = scan(GTIN2, "MISS1")
MISS2 = scan(GTIN4, "MISS2")
RULE_HIT = rule("R1", gtin=GTIN3)


def _contact_rows(body):
    return [
        (
            c["missed_item"]["serial"],
            c["hit_item"]["serial"],
            c["location"],
            c["start_time"],
            c["end_time"],
        )
        for c in body["contacts"]
    ]


def test_contacts_absent_without_contact_until():
    events = [
        transfer(MISS1, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(HIT, "2026-09-01T00:01:00Z", "WH-A"),
    ]
    body = post([HIT, MISS1], [RULE_HIT], events)
    assert "contacts" not in body
    assert body["items"] == [
        {"gtin": GTIN3, "serial": "HIT", "locations": ["WH-A"], "rule_ids": ["R1"]}
    ]


def test_contact_until_must_carry_timezone():
    events = [transfer(HIT, "2026-09-01T00:00:00Z", "WH-A")]
    resp = client.post(
        "/v1/recall/location-trace",
        json={
            "scans": [HIT],
            "rules": [RULE_HIT],
            "events": events,
            "contact_until": "2026-09-01T01:00:00",
        },
    )
    assert resp.status_code == 422


def test_contact_until_before_latest_event_returns_422():
    events = [
        transfer(HIT, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(HIT, "2026-09-01T05:00:00Z", "WH-B", from_loc="WH-A"),
    ]
    # 早于最晚事件(可带非 UTC 时区),整单 422
    resp = client.post(
        "/v1/recall/location-trace",
        json={
            "scans": [HIT],
            "rules": [RULE_HIT],
            "events": events,
            "contact_until": "2026-09-01T12:30:00+08:00",  # UTC 04:30 < 05:00
        },
    )
    assert resp.status_code == 422
    assert "contact_until" in resp.json()["detail"]
    # 恰好等于最晚事件时刻合法(左闭右开,无正时长共处时为空)
    body = post([HIT], [RULE_HIT], events, contact_until="2026-09-01T05:00:00Z")
    assert body["contacts"] == []


def test_contacts_basic_direct_overlap_and_utc_output():
    events = [
        transfer(MISS1, "2026-09-01T00:02:00Z", "WH-A"),
        transfer(HIT, "2026-09-01T00:01:00Z", "WH-A"),
        transfer(HIT, "2026-09-01T02:00:00Z", "WH-B", from_loc="WH-A"),
    ]
    body = post(
        [HIT, MISS1], [RULE_HIT], events, contact_until="2026-09-01T03:00:00Z"
    )
    # 共处自较晚到达的 00:02 起(00:01 时 MISS1 持有人未知),HIT 离开时闭合
    assert _contact_rows(body) == [
        ("MISS1", "HIT", "WH-A", "2026-09-01T00:02:00Z", "2026-09-01T02:00:00Z")
    ]


def test_contacts_same_instant_all_events_applied_first():
    # 同刻事件先全部处理:同刻到达 WH-A 即共处,段从该刻开始
    events = [
        transfer(MISS1, "2026-09-01T08:00:00+08:00", "WH-A"),
        transfer(HIT, "2026-09-01T08:00:00+08:00", "WH-A"),
    ]
    body = post(
        [HIT, MISS1], [RULE_HIT], events, contact_until="2026-09-01T09:00:00+08:00"
    )
    assert _contact_rows(body) == [
        ("MISS1", "HIT", "WH-A", "2026-09-01T00:00:00Z", "2026-09-01T01:00:00Z")
    ]
    # 同刻先后到达不同地点:本组全部处理后才评估,不产生零时长段
    events2 = [
        transfer(HIT, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(MISS1, "2026-09-01T00:00:00Z", "WH-B"),
    ]
    body2 = post(
        [HIT, MISS1], [RULE_HIT], events2, contact_until="2026-09-01T01:00:00Z"
    )
    assert body2["contacts"] == []


def test_contacts_container_transfer_carries_descendant():
    # 命中件装入未命中托盘后随整托移动:与另一件未命中实物在 WH-B 共处
    events = [
        *ALL_AT_A,  # PALLET / CASE / ITEM 各自在 WH-A 首次交接
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        pack(CASE, ITEM, "2026-09-01T02:00:00Z"),
        transfer(PALLET, "2026-09-01T03:00:00Z", "WH-B", from_loc="WH-A"),
        # 未命中的 CASE 此时随容器在 WH-B;规则只命中 ITEM(GTIN3)
    ]
    body = post(
        [PALLET, CASE, ITEM],
        [rule("R1", gtin=GTIN3)],
        events,
        contact_until="2026-09-01T04:00:00Z",
    )
    rows = _contact_rows(body)
    # CASE 与 ITEM:WH-A 段(装入后同在 A,随动前后都在同地)与 WH-B 段相接不合并(地点不同)
    assert ("CASE", "ITEM", "WH-A", "2026-09-01T00:02:00Z", "2026-09-01T03:00:00Z") in rows
    assert ("CASE", "ITEM", "WH-B", "2026-09-01T03:00:00Z", "2026-09-01T04:00:00Z") in rows
    # PALLET 也与 ITEM 同地(直接配对,不做传递之外的推断)
    assert ("PALLET", "ITEM", "WH-B", "2026-09-01T03:00:00Z", "2026-09-01T04:00:00Z") in rows


def test_contacts_unpack_keeps_location():
    events = [
        *ALL_AT_A,
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        pack(CASE, ITEM, "2026-09-01T02:00:00Z"),
        transfer(PALLET, "2026-09-01T03:00:00Z", "WH-B", from_loc="WH-A"),
        unpack(CASE, ITEM, "2026-09-01T04:00:00Z"),  # 取出保留 WH-B
    ]
    body = post(
        [PALLET, CASE, ITEM],
        [rule("R1", gtin=GTIN3)],
        events,
        contact_until="2026-09-01T05:00:00Z",
    )
    # 取出后 ITEM 仍在 WH-B,与 PALLET(当前持有人 WH-B)继续同地,WH-B 时段相接合并
    rows = [r for r in _contact_rows(body) if r[0] == "PALLET"]
    assert rows == [
        ("PALLET", "ITEM", "WH-A", "2026-09-01T00:02:00Z", "2026-09-01T03:00:00Z"),
        ("PALLET", "ITEM", "WH-B", "2026-09-01T03:00:00Z", "2026-09-01T05:00:00Z"),
    ]


def test_contacts_no_transitive_spread_and_unknown_holder():
    # HIT 在 WH-A 与 MISS1 共处;MISS1 随后到 WH-B 与从未命中的 MISS2 同地,
    # MISS2 不得经 MISS1 与 HIT 产生共处
    events = [
        transfer(HIT, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(MISS1, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(MISS2, "2026-09-01T00:00:00Z", "WH-B"),
        transfer(MISS1, "2026-09-01T01:00:00Z", "WH-B", from_loc="WH-A"),
    ]
    body = post(
        [HIT, MISS1, MISS2],
        [RULE_HIT],
        events,
        contact_until="2026-09-01T02:00:00Z",
    )
    assert _contact_rows(body) == [
        ("MISS1", "HIT", "WH-A", "2026-09-01T00:00:00Z", "2026-09-01T01:00:00Z")
    ]

    # MISS2 从未交接(持有人未知):即使 HIT 之后到达 WH-B 也不计
    events2 = [
        transfer(HIT, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(HIT, "2026-09-01T01:00:00Z", "WH-B", from_loc="WH-A"),
    ]
    body2 = post(
        [HIT, MISS2],
        [RULE_HIT],
        events2,
        contact_until="2026-09-01T02:00:00Z",
    )
    assert body2["contacts"] == []


def test_contacts_failed_events_have_no_effect():
    events = [
        transfer(HIT, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(MISS1, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(HIT, "2026-09-01T01:00:00Z", "WH-X", from_loc="WH-Z"),  # 链断裂,失败
    ]
    body = post(
        [HIT, MISS1], [RULE_HIT], events, contact_until="2026-09-01T02:00:00Z"
    )
    assert [e["index"] for e in body["errors"]] == [2]
    # 失败不改变持有人,WH-A 共处连续并合并为一段
    assert _contact_rows(body) == [
        ("MISS1", "HIT", "WH-A", "2026-09-01T00:00:00Z", "2026-09-01T02:00:00Z")
    ]


def test_contacts_adjacent_merged_gap_kept_separate():
    events = [
        transfer(HIT, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(MISS1, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(HIT, "2026-09-01T01:00:00Z", "WH-B", from_loc="WH-A"),
        transfer(HIT, "2026-09-01T02:00:00Z", "WH-A", from_loc="WH-B"),  # 回到 A
    ]
    body = post(
        [HIT, MISS1], [RULE_HIT], events, contact_until="2026-09-01T03:00:00Z"
    )
    # MISS1 一直在 WH-A:两段 [00,01) 与 [02,03) 不相接,不合并
    assert _contact_rows(body) == [
        ("MISS1", "HIT", "WH-A", "2026-09-01T00:00:00Z", "2026-09-01T01:00:00Z"),
        ("MISS1", "HIT", "WH-A", "2026-09-01T02:00:00Z", "2026-09-01T03:00:00Z"),
    ]


def test_contacts_sorted_by_identities_start_location():
    events = [
        transfer(HIT, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(MISS2, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(MISS1, "2026-09-01T00:00:00Z", "WH-A"),
    ]
    body = post(
        [HIT, MISS1, MISS2],
        [RULE_HIT],
        events,
        contact_until="2026-09-01T01:00:00Z",
    )
    # 未命中实物身份排序:GTIN2/MISS1 在 GTIN4/MISS2 之前
    assert [c["missed_item"]["serial"] for c in body["contacts"]] == ["MISS1", "MISS2"]
    for c in body["contacts"]:
        assert c["hit_item"] == {"gtin": GTIN3, "serial": "HIT"}


def test_contacts_empty_when_no_co_location():
    events = [
        transfer(HIT, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(MISS1, "2026-09-01T00:00:00Z", "WH-B"),
    ]
    body = post(
        [HIT, MISS1], [RULE_HIT], events, contact_until="2026-09-01T01:00:00Z"
    )
    assert body["contacts"] == []


# ---------- 封签 / 拆签 ----------


def test_sealed_items_absent_without_seal_events():
    body = post([PALLET], [rule("R1")], ALL_AT_A[:1])
    assert "sealed_items" not in body
    assert body["items"] == [
        {"gtin": GTIN, "serial": "PALLET", "locations": ["WH-A"], "rule_ids": ["R1"]}
    ]


def test_seal_requires_known_holder_and_succeeds():
    events = [
        seal(PALLET, "SEAL-1", "2026-09-01T00:00:00Z"),  # 尚无持有人,失败
        transfer(PALLET, "2026-09-01T00:01:00Z", "WH-A"),
        seal(PALLET, "SEAL-1", "2026-09-01T00:02:00Z"),  # 成功
    ]
    body = post([PALLET], [rule("R1")], events)
    assert [e["index"] for e in body["errors"]] == [0]
    assert "持有人未知" in body["errors"][0]["reason"]
    assert body["sealed_items"] == [
        {"gtin": GTIN, "serial": "PALLET", "seal_id": "SEAL-1"}
    ]
    # 封签不改地点 / 持有人
    assert body["items"][0]["locations"] == ["WH-A"]


def test_seal_unseal_and_reseal():
    events = [
        transfer(PALLET, "2026-09-01T00:00:00Z", "WH-A"),
        seal(PALLET, "S1", "2026-09-01T00:01:00Z"),
        seal(PALLET, "S2", "2026-09-01T00:02:00Z"),  # 重复封签,失败
        unseal(PALLET, "WRONG", "2026-09-01T00:03:00Z"),  # 封签号不符,失败
        unseal(PALLET, "S1", "2026-09-01T00:04:00Z"),  # 成功
        unseal(PALLET, "S1", "2026-09-01T00:05:00Z"),  # 已拆签,失败
        seal(PALLET, "S3", "2026-09-01T00:06:00Z"),  # 重新封签成功
    ]
    body = post([PALLET], [rule("R1")], events)
    assert [e["index"] for e in body["errors"]] == [2, 3, 5]
    assert "已处于封签状态" in body["errors"][0]["reason"]
    assert "不一致" in body["errors"][1]["reason"]
    assert "未封签" in body["errors"][2]["reason"]
    # 最终仍封签的是 S3
    assert body["sealed_items"] == [
        {"gtin": GTIN, "serial": "PALLET", "seal_id": "S3"}
    ]


def test_nested_seal_must_unseal_outside_in():
    events = [
        *ALL_AT_A,
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        pack(CASE, ITEM, "2026-09-01T02:00:00Z"),
        seal(ITEM, "SI", "2026-09-01T03:00:00Z"),  # 内层先封
        seal(CASE, "SC", "2026-09-01T04:00:00Z"),  # 再封外层:合法(祖先向上看)
        seal(PALLET, "SP", "2026-09-01T05:00:00Z"),  # 最外层:合法
        unseal(ITEM, "SI", "2026-09-01T06:00:00Z"),  # 外层仍封,失败
        unseal(CASE, "SC", "2026-09-01T07:00:00Z"),  # 托盘仍封,失败
        unseal(PALLET, "SP", "2026-09-01T08:00:00Z"),  # 由外向内:成功
        unseal(CASE, "SC", "2026-09-01T09:00:00Z"),
        unseal(ITEM, "SI", "2026-09-01T10:00:00Z"),
    ]
    body = post([PALLET, CASE, ITEM], [rule("R1", gtin=GTIN3)], events)
    assert [e["index"] for e in body["errors"]] == [8, 9]
    assert "由外向内" in body["errors"][0]["reason"]
    # 全部拆签后 sealed_items 为空(但字段仍存在,因含封签事件)
    assert body["sealed_items"] == []
    assert body["items"][0]["locations"] == ["WH-A"]  # 封签 / 拆签不记地点


def test_sealed_items_sorted_and_includes_unrecalled():
    a = scan(GTIN, "A")
    b = scan(GTIN2, "B")
    events = [
        transfer(a, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(b, "2026-09-01T00:01:00Z", "WH-A"),
        seal(b, "SB", "2026-09-01T00:02:00Z"),  # 未命中召回,仍出现在 sealed_items
        seal(a, "SA", "2026-09-01T00:03:00Z"),
    ]
    # 规则只命中 GTIN
    body = post([a, b], [rule("R1")], events)
    assert body["errors"] == []
    # 按 GTIN、序列号排序:GTIN/A 在 GTIN2/B 之前
    assert body["sealed_items"] == [
        {"gtin": GTIN, "serial": "A", "seal_id": "SA"},
        {"gtin": GTIN2, "serial": "B", "seal_id": "SB"},
    ]
    # items 仅命中召回的 A
    assert [i["serial"] for i in body["items"]] == ["A"]


def test_pack_into_sealed_container_or_ancestor_fails():
    extra = scan(GTIN4, "EXTRA")
    events = [
        *ALL_AT_A,
        transfer(extra, "2026-09-01T00:03:00Z", "WH-A"),
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        seal(PALLET, "SP", "2026-09-01T02:00:00Z"),
        # 父件自身封签:装入 / 取出均失败
        pack(PALLET, extra, "2026-09-01T03:00:00Z"),
        unpack(PALLET, CASE, "2026-09-01T04:00:00Z"),
        unseal(PALLET, "SP", "2026-09-01T05:00:00Z"),
        seal(CASE, "SC", "2026-09-01T06:00:00Z"),
        # CASE 的祖先?此时 CASE 父件 PALLET 已拆签;封 CASE 后向 CASE 装入被自身封签阻断
        pack(CASE, extra, "2026-09-01T07:00:00Z"),
    ]
    body = post([PALLET, CASE, ITEM, extra], [rule("R1", gtin=GTIN2)], events)
    assert [e["index"] for e in body["errors"]] == [6, 7, 10]
    assert "父件" in body["errors"][0]["reason"] and "已封签" in body["errors"][0]["reason"]
    assert "取出失败" in body["errors"][1]["reason"]
    assert "装入失败" in body["errors"][2]["reason"]


def test_pack_unpack_fails_when_ancestor_sealed_not_direct_parent():
    extra = scan(GTIN4, "EXTRA")
    events = [
        *ALL_AT_A,
        transfer(extra, "2026-09-01T00:03:00Z", "WH-A"),
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),
        pack(CASE, ITEM, "2026-09-01T02:00:00Z"),
        seal(PALLET, "SP", "2026-09-01T03:00:00Z"),
        # CASE 自身未封签但祖先 PALLET 已封:装入 / 取出都失败
        pack(CASE, extra, "2026-09-01T04:00:00Z"),
        unpack(CASE, ITEM, "2026-09-01T05:00:00Z"),
    ]
    body = post([PALLET, CASE, ITEM, extra], [rule("R1", gtin=GTIN3)], events)
    assert [e["index"] for e in body["errors"]] == [7, 8]
    assert "包装祖先" in body["errors"][0]["reason"]
    # 失败不改变树:拆签托盘后,ITM 仍随整托交接
    events2 = events + [
        unseal(PALLET, "SP", "2026-09-01T06:00:00Z"),
        unpack(CASE, ITEM, "2026-09-01T07:00:00Z"),  # 祖先不再封签,取出成功
    ]
    body2 = post([PALLET, CASE, ITEM, extra], [rule("R1", gtin=GTIN3)], events2)
    assert [e["index"] for e in body2["errors"]] == [7, 8]
    assert body2["sealed_items"] == []


def test_sealed_subtree_packs_moves_and_root_transfer_carries():
    # 封签子件仍可整体装入、取出;根件交接带动封签后代
    events = [
        *ALL_AT_A,
        seal(CASE, "SC", "2026-09-01T00:03:00Z"),  # CASE 已封签
        pack(PALLET, CASE, "2026-09-01T01:00:00Z"),  # 封签子件整体装入:父件 PALLET 未封,成功
        transfer(PALLET, "2026-09-01T02:00:00Z", "WH-B", from_loc="WH-A"),  # 带动封签的 CASE
        unpack(PALLET, CASE, "2026-09-01T03:00:00Z"),  # 父件未封,整体取出成功
    ]
    body = post([PALLET, CASE, ITEM], [rule("R1", gtin=GTIN2)], events)
    assert body["errors"] == []
    assert body["items"][0]["locations"] == ["WH-A", "WH-B"]  # CASE 随根件移动
    assert body["sealed_items"] == [
        {"gtin": GTIN2, "serial": "CASE", "seal_id": "SC"}
    ]


def test_seal_event_scan_validation_uses_original_index():
    ghost = scan(GTIN4, "GHOST")
    events = [
        transfer(PALLET, "2026-09-01T00:00:00Z", "WH-A"),
        {"action": "seal", "scan": "01" + GTIN + "99",
         "seal_id": "S", "occurred_at": "2026-09-01T00:01:00Z"},  # 扫描非法
        seal(ghost, "S", "2026-09-01T00:02:00Z"),  # 不在库存
    ]
    body = post([PALLET], [rule("R1")], events)
    assert [e["index"] for e in body["errors"]] == [1, 2]
    assert "扫描非法" in body["errors"][0]["reason"]
    assert "不在库存扫描中" in body["errors"][1]["reason"]
    # 出现封签事件(虽全失败)仍附 sealed_items,且为空
    assert body["sealed_items"] == []


def test_seal_event_shape_validation():
    base = {
        "scans": [scan()],
        "rules": [rule("R1")],
    }
    _expect_422({**base, "events": [
        {"action": "seal", "scan": scan(), "seal_id": "",
         "occurred_at": "2026-09-01T00:00:00Z"}]})  # 空 seal_id
    _expect_422({**base, "events": [
        {"action": "unseal", "scan": scan(),
         "occurred_at": "2026-09-01T00:00:00Z"}]})  # 缺 seal_id
    _expect_422({**base, "events": [
        {"action": "seal", "scan": scan(), "seal_id": "S",
         "occurred_at": "2026-09-01T00:00:00Z", "extra": 1}]})  # 多余字段
    _expect_422({**base, "events": [
        {"action": "seal", "scan": scan(), "seal_id": "S",
         "occurred_at": "2026-09-01T00:00:00"}]})  # 缺时区


def test_failed_seal_or_unseal_changes_nothing_and_continues():
    events = [
        transfer(PALLET, "2026-09-01T00:00:00Z", "WH-A"),
        seal(PALLET, "S1", "2026-09-01T00:01:00Z"),
        unseal(PALLET, "NOPE", "2026-09-01T00:02:00Z"),  # 失败:仍封签
        transfer(PALLET, "2026-09-01T00:03:00Z", "WH-B", from_loc="WH-A"),  # 交接不受封签影响
    ]
    body = post([PALLET], [rule("R1")], events)
    assert [e["index"] for e in body["errors"]] == [2]
    assert body["items"][0]["locations"] == ["WH-A", "WH-B"]
    assert body["sealed_items"] == [
        {"gtin": GTIN, "serial": "PALLET", "seal_id": "S1"}
    ]


def test_sealed_items_with_contact_until_coexist():
    events = [
        transfer(PALLET, "2026-09-01T00:00:00Z", "WH-A"),
        transfer(CASE, "2026-09-01T00:01:00Z", "WH-A"),
        seal(PALLET, "SP", "2026-09-01T00:02:00Z"),
    ]
    body = post(
        [PALLET, CASE],
        [rule("R1", gtin=GTIN2)],
        events,
        contact_until="2026-09-01T01:00:00Z",
    )
    assert body["errors"] == []
    assert body["sealed_items"] == [
        {"gtin": GTIN, "serial": "PALLET", "seal_id": "SP"}
    ]
    # contacts 仍按成功事件计算:PALLET(未命中)与 CASE(命中)在 WH-A 共处
    assert [
        (c["missed_item"]["serial"], c["hit_item"]["serial"], c["location"])
        for c in body["contacts"]
    ] == [("PALLET", "CASE", "WH-A")]


# ---------- 旧接口回归 ----------
def test_existing_endpoints_still_available():
    assert client.get("/healthz").json() == {"status": "ok"}
    custody_event = {
        "scan": scan(),
        "occurred_at": "2026-09-01T00:00:00Z",
        "from_location": None,
        "to_location": "WH-A",
    }
    resp = client.post("/v1/custody/audit", json={"events": [custody_event]})
    assert resp.status_code == 200
    assert resp.json()["items"][0]["holder"] == "WH-A"
    resp = client.post(
        "/v1/packing/audit",
        json={"operations": [{"action": "pack",
                              "parent_scan": scan(GTIN, "P"),
                              "child_scan": scan(GTIN2, "C")}]},
    )
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 2
