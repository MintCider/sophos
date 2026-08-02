"""Live adapter registry keyed by stable database binding/account IDs."""

from sophos.platform import (
    AdapterUnavailableError,
    Capability,
    CapabilityUnavailableError,
    MessagingAdapter,
)


class AdapterRouter:
    def __init__(self) -> None:
        self._by_binding: dict[int, MessagingAdapter] = {}
        self._by_account: dict[int, MessagingAdapter] = {}

    def register(self, adapter: MessagingAdapter) -> None:
        self._by_binding[adapter.binding_id] = adapter
        self._by_account[adapter.account_id] = adapter

    def unregister(self, adapter: MessagingAdapter) -> None:
        if self._by_binding.get(adapter.binding_id) is adapter:
            self._by_binding.pop(adapter.binding_id, None)
        if self._by_account.get(adapter.account_id) is adapter:
            self._by_account.pop(adapter.account_id, None)

    def for_account(self, account_id: int, capability: Capability) -> MessagingAdapter:
        adapter = self._by_account.get(account_id)
        if adapter is None:
            raise AdapterUnavailableError(f"no live adapter for account {account_id}")
        self._require_capability(adapter, capability)
        return adapter

    def for_binding(self, binding_id: int, capability: Capability) -> MessagingAdapter:
        adapter = self._by_binding.get(binding_id)
        if adapter is None:
            raise AdapterUnavailableError(f"no live adapter for binding {binding_id}")
        self._require_capability(adapter, capability)
        return adapter

    @staticmethod
    def _require_capability(adapter: MessagingAdapter, capability: Capability) -> None:
        if capability not in adapter.capabilities:
            raise CapabilityUnavailableError(
                f"adapter binding {adapter.binding_id} does not support {capability.value}"
            )

