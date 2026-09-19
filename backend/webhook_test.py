import webhook


@webhook.register_handler
def print_event(event):
    print("\n==============================")
    print("WEBHOOK RECEIVED!")
    print("==============================")
    print(event)


if __name__ == "__main__":
    print("Webhook listener running...")
    webhook.run(host="0.0.0.0", port=5000)
