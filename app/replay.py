"""实物重放内核:交接 / 装入 / 取出共用同一套逐事件状态机。

三种旧接口(交接审计、装拆包装审计、召回实物地点追溯)与召回隔离计划都在本
内核之上做各自的**校验、适配与结果整理**,不再各维护一套状态转换:

- 身份:实物一律以 (GTIN, AI 21 序列号) 标识(`ItemKey`);
- 父子关系:`parent_of[child] = parent` 的直接父子边,装入加边、取出删边;
- 持有人:节点 `holder`(尚未成功交接为 None),根件交接时带动全部后代;
- 成功事件提交:`success_indices` 记录成功事件 / 操作的原下标,只有成功提交
  的实物才进入成功涉及集合 `involved`;
- 地点历史:节点 `locations` 按首次到达顺序去重(仅地点追溯需要)。

内核**不统一成最严模式**,差异由 `ReplayConfig` 控制:

- 独立装拆(`packing`):不要求库存或持有人,`unpack` 的双方只要是成功涉及的
  已知实物即可;失败操作不会把任何实物加入成功涉及集合。
- 独立交接(`custody`):无包装树,批号 / 有效期基准取**首次成功事件**
  (经提交前回调校验,失败不产生任何半次更新)。
- 地点追溯(`trace`):以库存为基准(节点全部预先注册);装入双方须已有**相同
  持有人**;交接只作用于根件并带动全部后代;取出保留当时持有人;封签 / 拆签
  (`seal` / `unseal`)随事件审计,已封签(含封签祖先)的容器禁止再装入 / 取出。

每个命令先完成全部校验,再一次性提交:失败命令不会留下父子边、持有人、成功
下标或地点历史的半次更新,后续命令继续从失败前状态处理。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable

# 实物标识:GTIN + AI 21 序列号
ItemKey = tuple[str, str]


def item_label(key: ItemKey) -> str:
    return f"实物 (GTIN {key[0]}, 序列号 '{key[1]}')"


def fmt_value(value: object) -> str:
    return "(缺失)" if value is None else f"'{value}'"


# ---------- 内核配置 ----------


@dataclass(frozen=True)
class ReplayConfig:
    """内核重放策略;三种接口各取一套,内核本身不做接口专属校验。

    - packing:独立装拆,无库存、无持有人要求;
    - custody:独立交接,无包装树;
    - trace:地点追溯,以预注册库存节点为基准,装入要求双方已有相同持有人。
    """

    mode: str  # "packing" | "custody" | "trace"
    require_holders_for_pack: bool = False  # 装入是否要求双方已有成功交接
    transfer_roots_only: bool = False  # 交接是否仅作用于无父件实物
    propagate_transfer: bool = False  # 根件交接是否带动全部后代
    register_on_pack: bool = True  # 装入时是否允许注册此前未见过的实物
    unknown_on_unpack_fails: bool = True  # 取出时未在成功涉及集合中的实物是否报错
    seal_aware: bool = False  # 是否维护封签(seal / unseal,并在装拆时校验封签)

    @classmethod
    def packing(cls) -> "ReplayConfig":
        """独立装拆:不要求库存或持有人,新父件随装入登记。"""
        return cls(mode="packing")

    @classmethod
    def custody(cls) -> "ReplayConfig":
        """独立交接:没有包装 / 持有人传播语义。"""
        return cls(mode="custody")

    @classmethod
    def trace(cls) -> "ReplayConfig":
        """地点追溯:库存基准、同持有人才可装入、根件交接带动后代、封签随事件审计。"""
        return cls(
            mode="trace",
            require_holders_for_pack=True,
            transfer_roots_only=True,
            propagate_transfer=True,
            register_on_pack=False,
            unknown_on_unpack_fails=False,
            seal_aware=True,
        )


# ---------- 节点与失败结果 ----------


@dataclass
class KernelNode:
    """单件实物的内核状态;适配层可通过 meta 存放接口专属基准(如批号 / 有效期)。"""

    key: ItemKey
    holder: str | None = None  # 当前持有人;尚未成功交接时为 None
    locations: list[str] = field(default_factory=list)  # 按首次到达顺序去重的地点历史
    success_indices: list[int] = field(default_factory=list)  # 成功命令的原下标
    seal_id: str | None = None  # 当前封签 ID;未封签为 None
    meta: dict[str, object] = field(default_factory=dict)

    def arrive(self, location: str) -> None:
        """到达一个保管地点(去重,保留首次到达顺序)。"""
        if location not in self.locations:
            self.locations.append(location)


# 机器可读的失败码;适配层据此(或直接使用携带的上下文)整理旧接口 reason 文案
FIRST_TRANSFER_FROM_NOT_NULL = "first_transfer_from_not_null"
HOLDER_MISMATCH = "holder_mismatch"
HAS_PARENT_TRANSFER = "has_parent_transfer"
CHILD_HAS_PARENT = "child_has_parent"
SAME_ITEM = "same_item"
PACK_CYCLE = "pack_cycle"
HOLDER_MISSING = "holder_missing"
HOLDER_DIFFERS = "holder_differs"
UNKNOWN_PARENT = "unknown_parent"
UNKNOWN_CHILD = "unknown_child"
NOT_DIRECT_PARENT = "not_direct_parent"
UNKNOWN_ITEM = "unknown_item"
PRECHECK_REJECTED = "precheck_rejected"
SEAL_HOLDER_MISSING = "seal_holder_missing"
ALREADY_SEALED = "already_sealed"
SEALED_ANCESTOR = "sealed_ancestor"
NOT_SEALED = "not_sealed"
SEAL_ID_MISMATCH = "seal_id_mismatch"


@dataclass
class ReplayFailure:
    """一条失败命令:机器可读 code、原下标与整理文案所需的上下文。"""

    code: str
    index: int
    parent: ItemKey | None = None
    child: ItemKey | None = None
    item: ItemKey | None = None
    expected: str | None = None  # 交接链校验时的期望交出方(已格式化)
    actual: str | None = None  # 实际交出方(已格式化,如 "null" 或 "'WH-X'")
    detail: str | None = None  # 提交前回调等场景的直接文案


# 提交前回调:结构校验通过、状态尚未改动时调用;返回 ReplayFailure 即否决提交。
# 交接审计用它在首次成功事件上固化批号 / 有效期基准、在后续事件上比对。
PreTransferCheck = Callable[
    ["ReplayKernel", ItemKey, int, bool, "KernelNode | None"],
    "ReplayFailure | None",
]


# ---------- 内核 ----------


class ReplayKernel:
    """逐事件推进的实物重放内核:transfer / pack / unpack 三类命令。

    典型用法:

        kernel = ReplayKernel(ReplayConfig.packing())
        failure = kernel.pack(index, parent_key, child_key)
        if failure is not None:
            ...  # 状态与失败前完全一致
    """

    def __init__(self, config: ReplayConfig) -> None:
        self.config = config
        self.nodes: dict[ItemKey, KernelNode] = {}
        # 直接父子边:parent_of[child] = parent
        self.parent_of: dict[ItemKey, ItemKey] = {}
        # 反向邻接,随装 / 拆增量维护,供后代 BFS 使用
        self.children_of: dict[ItemKey, list[ItemKey]] = {}
        # 成功命令涉及的实物集合(只有成功提交才加入)
        self.involved: set[ItemKey] = set()

    # ----- 观察 / 预注册 -----

    def has_node(self, key: ItemKey) -> bool:
        return key in self.nodes

    def get(self, key: ItemKey) -> KernelNode | None:
        return self.nodes.get(key)

    def register(self, key: ItemKey, **meta: object) -> KernelNode:
        """预先注册一个实物(地点追溯的库存基准);已存在时原样返回。"""
        node = self.nodes.get(key)
        if node is None:
            node = KernelNode(key=key)
            self.nodes[key] = node
        node.meta.update(meta)
        return node

    def descendants(self, root: ItemKey) -> list[ItemKey]:
        """返回包装树中 root 的全部后代(不含自身),按当前树结构 BFS 展开。"""
        result: list[ItemKey] = []
        queue: deque[ItemKey] = deque(self.children_of.get(root, []))
        while queue:
            node = queue.popleft()
            result.append(node)
            queue.extend(self.children_of.get(node, []))
        return result

    def root_of(self, key: ItemKey) -> ItemKey:
        """沿父链走到最外层容器。"""
        node = key
        guard: set[ItemKey] = set()
        while node in self.parent_of and node not in guard:
            guard.add(node)
            node = self.parent_of[node]
        return node

    def sealed_ancestor(self, key: ItemKey) -> ItemKey | None:
        """沿父链(不含自身)向上找到的最近封签实物;没有时返回 None。"""
        node = self.parent_of.get(key)
        guard: set[ItemKey] = set()
        while node is not None and node not in guard:
            ancestor_node = self.nodes.get(node)
            if ancestor_node is not None and ancestor_node.seal_id is not None:
                return node
            guard.add(node)
            node = self.parent_of.get(node)
        return None

    def sealed_self_or_ancestor(self, key: ItemKey) -> ItemKey | None:
        """key 自身或其沿父链向上的最近封签实物;都没有封签时返回 None。"""
        node = self.nodes.get(key)
        if node is not None and node.seal_id is not None:
            return key
        return self.sealed_ancestor(key)

    # ----- 交接 -----

    def transfer(
        self,
        index: int,
        key: ItemKey,
        from_location: str | None,
        to_location: str,
        precheck: PreTransferCheck | None = None,
    ) -> ReplayFailure | None:
        """交接一件无父件(根)实物;成功后可按配置带动全部后代换持有人。

        首次成功交接(节点尚无持有人)from_location 须为 null;此后须等于当前
        持有人。结构 / 交接链 / 提交前回调全部通过后才一次性提交持有人、成功下标
        与地点历史。
        """
        node = self.nodes.get(key)
        first = node is None or node.holder is None

        if self.config.transfer_roots_only and key in self.parent_of:
            return ReplayFailure(
                code=HAS_PARENT_TRANSFER,
                index=index,
                item=key,
                parent=self.parent_of[key],
            )

        current_holder = None if node is None else node.holder
        if from_location != current_holder:
            actual = "null" if from_location is None else f"'{from_location}'"
            expected = "null(实物尚无成功交接)" if current_holder is None else (
                f"'{current_holder}'"
            )
            return ReplayFailure(
                code=(
                    FIRST_TRANSFER_FROM_NOT_NULL
                    if first and from_location is not None
                    else HOLDER_MISMATCH
                ),
                index=index,
                item=key,
                expected=expected,
                actual=actual,
            )

        if precheck is not None:
            failure = precheck(self, key, index, first, node)
            if failure is not None:
                return failure

        # ---- 全部校验通过,一次性提交 ----
        if node is None:
            node = KernelNode(key=key)
            self.nodes[key] = node
        affected = [key, *self.descendants(key)] if self.config.propagate_transfer else [key]
        for affected_key in affected:
            affected_node = self.nodes[affected_key]
            affected_node.holder = to_location
            affected_node.arrive(to_location)
        # 成功下标只记被交接的根件自身:后代是随动,并未各自发生一条事件
        node.success_indices.append(index)
        self.involved.add(key)
        return None

    # ----- 装入 -----

    def pack(
        self, index: int, parent_key: ItemKey, child_key: ItemKey
    ) -> ReplayFailure | None:
        """装入:加一条 child -> parent 直接父子边。

        独立装拆允许在成功装入时登记新实物;地点追溯要求双方都是库存已知节点、
        已有相同持有人。成环、自装、重复装入一律失败且不留任何更新。
        """
        if child_key in self.parent_of:
            return ReplayFailure(
                code=CHILD_HAS_PARENT,
                index=index,
                parent=self.parent_of[child_key],
                child=child_key,
            )
        if child_key == parent_key:
            return ReplayFailure(code=SAME_ITEM, index=index, parent=parent_key, child=child_key)

        parent_node = self.nodes.get(parent_key)
        child_node = self.nodes.get(child_key)

        if self.config.require_holders_for_pack:
            parent_holder = parent_node.holder if parent_node is not None else None
            child_holder = child_node.holder if child_node is not None else None
            if parent_holder is None or child_holder is None:
                return ReplayFailure(
                    code=HOLDER_MISSING,
                    index=index,
                    parent=parent_key,
                    child=child_key,
                    detail="父件" if parent_holder is None else "子件",
                )
            if parent_holder != child_holder:
                return ReplayFailure(
                    code=HOLDER_DIFFERS,
                    index=index,
                    parent=parent_key,
                    child=child_key,
                    expected=parent_holder,
                    actual=child_holder,
                )

        if self.config.seal_aware:
            # 父件自身或其任一祖先已封签时,不能再装入;封签子件仍可整体装入
            sealed = self.sealed_self_or_ancestor(parent_key)
            if sealed is not None:
                return ReplayFailure(
                    code=SEALED_ANCESTOR,
                    index=index,
                    parent=parent_key,
                    child=child_key,
                    item=sealed,
                    detail="装入",
                )

        # 沿父链向上检查 child 是否为 parent 的祖先(防止自装 / 形成循环包装)
        ancestor = self.parent_of.get(parent_key)
        seen: set[ItemKey] = set()
        while ancestor is not None:
            if ancestor == child_key:
                return ReplayFailure(
                    code=PACK_CYCLE, index=index, parent=parent_key, child=child_key
                )
            if ancestor in seen:  # 防御性:正常维护下不会出现环
                break
            seen.add(ancestor)
            ancestor = self.parent_of.get(ancestor)

        if not self.config.register_on_pack:
            # trace:双方在调用前已由适配层确认是库存节点
            if parent_node is None:
                return ReplayFailure(code=UNKNOWN_PARENT, index=index, parent=parent_key)
            if child_node is None:
                return ReplayFailure(code=UNKNOWN_CHILD, index=index, child=child_key)

        # ---- 全部校验通过,一次性提交 ----
        if parent_node is None:
            parent_node = KernelNode(key=parent_key)
            self.nodes[parent_key] = parent_node
        if child_node is None:
            child_node = KernelNode(key=child_key)
            self.nodes[child_key] = child_node
        self.parent_of[child_key] = parent_key
        self.children_of.setdefault(parent_key, []).append(child_key)
        self.involved.add(child_key)
        self.involved.add(parent_key)
        child_node.success_indices.append(index)
        return None

    # ----- 取出 -----

    def unpack(
        self, index: int, parent_key: ItemKey, child_key: ItemKey
    ) -> ReplayFailure | None:
        """取出:删除 child 的当前直接父子边;子件保留当时持有人与地点历史。"""
        known = self.nodes if not self.config.unknown_on_unpack_fails else self.involved
        if parent_key not in known:
            return ReplayFailure(code=UNKNOWN_PARENT, index=index, parent=parent_key)
        if child_key not in known:
            return ReplayFailure(code=UNKNOWN_CHILD, index=index, child=child_key)

        current = self.parent_of.get(child_key)
        if current != parent_key:
            return ReplayFailure(
                code=NOT_DIRECT_PARENT,
                index=index,
                parent=parent_key,
                child=child_key,
                # 当前直接父件;None 表示子件当前无父件
                item=current,
            )

        if self.config.seal_aware:
            # 父件自身或其任一祖先已封签时,不能取出;封签子件仍可整体取出
            sealed = self.sealed_self_or_ancestor(parent_key)
            if sealed is not None:
                return ReplayFailure(
                    code=SEALED_ANCESTOR,
                    index=index,
                    parent=parent_key,
                    child=child_key,
                    item=sealed,
                    detail="取出",
                )

        # ---- 全部校验通过,一次性提交 ----
        del self.parent_of[child_key]
        siblings = self.children_of.get(parent_key)
        if siblings is not None:
            siblings.remove(child_key)
            if not siblings:
                del self.children_of[parent_key]
        # 取出不移除持有人 / 地点历史,也不重复记地点;两端仍属成功涉及实物
        self.involved.add(child_key)
        self.involved.add(parent_key)
        child_node = self.nodes[child_key]
        child_node.success_indices.append(index)
        return None

    # ----- 封签 / 拆签(仅地点追溯 trace 模式) -----

    def seal(self, index: int, key: ItemKey, seal_id: str) -> ReplayFailure | None:
        """封签一件实物:持有人已知、自身未封签且无封签祖先时成功。

        成功只在该实物节点上记录当前封签 ID,不改变持有人、包装关系与地点历史。
        """
        node = self.nodes.get(key)
        if node is None or node.holder is None:
            return ReplayFailure(code=SEAL_HOLDER_MISSING, index=index, item=key)
        if node.seal_id is not None:
            return ReplayFailure(
                code=ALREADY_SEALED, index=index, item=key, expected=node.seal_id
            )
        ancestor = self.sealed_ancestor(key)
        if ancestor is not None:
            return ReplayFailure(
                code=SEALED_ANCESTOR,
                index=index,
                item=key,
                parent=ancestor,
                detail="seal",
            )

        # ---- 全部校验通过,一次性提交 ----
        node.seal_id = seal_id
        node.success_indices.append(index)
        self.involved.add(key)
        return None

    def unseal(self, index: int, key: ItemKey, seal_id: str) -> ReplayFailure | None:
        """拆签一件实物:须匹配自身当前封签,且无封签祖先(由外向内逐层拆)。

        成功清除该实物节点的封签 ID,不改变持有人、包装关系与地点历史。
        """
        node = self.nodes.get(key)
        if node is None or node.seal_id is None:
            return ReplayFailure(code=NOT_SEALED, index=index, item=key)
        if node.seal_id != seal_id:
            return ReplayFailure(
                code=SEAL_ID_MISMATCH,
                index=index,
                item=key,
                expected=node.seal_id,
                actual=seal_id,
            )
        ancestor = self.sealed_ancestor(key)
        if ancestor is not None:
            return ReplayFailure(
                code=SEALED_ANCESTOR,
                index=index,
                item=key,
                parent=ancestor,
                detail="unseal",
            )

        # ---- 全部校验通过,一次性提交 ----
        node.seal_id = None
        node.success_indices.append(index)
        self.involved.add(key)
        return None
