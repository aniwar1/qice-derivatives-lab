"""独立交易日再次确认后的第二笔委托。"""

from .agent_replay_account import Account, position_value


class SecondEntryAccount(Account):
    shared_leg_liquidity = True

    def __init__(self, group, variant):
        super().__init__(group)
        self.execution_variant = variant

    def product_entry_allowed(self, day, decision, packet):
        held = [pos for pos in self.positions if pos["product"] == decision["product"]]
        if not held:
            return True
        if len(held) >= 2 or held[0]["day"] >= day:
            return False
        if self.execution_variant == "second_signal":
            return True
        position = held[0]
        if position["exit_reason"]:
            return False
        known = {o["id"]: o for o in packet["products"][decision["product"]]["options"]}
        side = "both" if len(decision["legs"]) == 2 else known[decision["legs"][0]]["side"]
        if position["side"] != side or any(leg not in known for leg in position["legs"]):
            return False
        marks = [known[leg].get("premium") for leg in position["legs"]]
        if any(not value or value <= 0 for value in marks):
            return False
        return position_value(position, marks) > position["cost"]
