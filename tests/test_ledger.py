from datetime import datetime, timezone

from app.services.ledger import PassiveLedgerTxn, compute_net_balance, validate_cash_ledger_integrity


def txn(date_str: str, type_: str, amount_native: float) -> PassiveLedgerTxn:
    return PassiveLedgerTxn(date=datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc), type=type_, amount_native=amount_native)


class TestComputeNetBalance:
    def test_empty_returns_zero(self) -> None:
        assert compute_net_balance([]) == 0

    def test_single_deposit(self) -> None:
        assert compute_net_balance([txn("2024-01-01", "DEPOSIT", 1000)]) == 1000

    def test_deposit_and_withdrawal_nets_the_difference(self) -> None:
        result = compute_net_balance([txn("2024-01-01", "DEPOSIT", 1000), txn("2024-02-01", "WITHDRAWAL", 300)])
        assert result == 700

    def test_order_independent(self) -> None:
        a = [txn("2024-01-01", "DEPOSIT", 500), txn("2024-02-01", "WITHDRAWAL", 100), txn("2024-03-01", "DEPOSIT", 200)]
        b = [a[2], a[0], a[1]]
        assert compute_net_balance(a) == compute_net_balance(b) == 600


class TestValidateCashLedgerIntegrity:
    def test_valid_deposit_then_smaller_withdrawal(self) -> None:
        result = validate_cash_ledger_integrity([txn("2024-01-01", "DEPOSIT", 1000), txn("2024-02-01", "WITHDRAWAL", 400)])
        assert result["valid"] is True

    def test_valid_withdrawal_to_exactly_zero(self) -> None:
        result = validate_cash_ledger_integrity([txn("2024-01-01", "DEPOSIT", 500), txn("2024-02-01", "WITHDRAWAL", 500)])
        assert result["valid"] is True

    def test_invalid_withdrawal_with_no_prior_deposit(self) -> None:
        result = validate_cash_ledger_integrity([txn("2024-01-01", "WITHDRAWAL", 100)])
        assert result["valid"] is False
        assert "error" in result

    def test_invalid_mid_sequence_even_if_final_net_is_positive(self) -> None:
        result = validate_cash_ledger_integrity(
            [
                txn("2024-01-01", "DEPOSIT", 100),
                txn("2024-02-01", "WITHDRAWAL", 200),
                txn("2024-03-01", "DEPOSIT", 500),
            ]
        )
        assert result["valid"] is False

    def test_sorts_by_date_before_checking_regardless_of_input_order(self) -> None:
        # Early WITHDRAWAL appears last in the input array, but is still
        # chronologically first and should still be caught as invalid.
        result = validate_cash_ledger_integrity(
            [txn("2024-03-01", "DEPOSIT", 500), txn("2024-01-01", "WITHDRAWAL", 100)]
        )
        assert result["valid"] is False
