# from datetime import datetime

# from payment_logic import (
#     calculate_charges,
#     can_charge,
#     calculate_parking_minutes,
#     process_payment
# )

# print("\n--- Complete Payment Test ---")

# entry_time = datetime(2026, 9, 19, 14, 0)
# exit_time = datetime(2026, 9, 19, 14, 5)

# payment = process_payment(
#     car_type="Normal",
#     entry_time=entry_time,
#     exit_time=exit_time,
#     at_exit=True,
#     already_paid=False
# )

# print(payment)

from payment_logic import (
    calculate_charges,
    can_charge,
    calculate_parking_minutes,
    process_payment,
    get_post_payment_action
)

print("\n--- Post Payment Test ---")

action = get_post_payment_action(True)
print("Successful payment:", action)

action = get_post_payment_action(False)
print("Failed payment:", action)