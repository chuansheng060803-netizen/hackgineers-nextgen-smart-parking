from flask import Flask, request

app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()

    print("\n==============================")
    print("WEBHOOK RECEIVED!")
    print("==============================")
    print(data)

    return {"status": "received"}, 200


if __name__ == "__main__":
    print("Webhook listener running...")
    app.run(host="0.0.0.0", port=5000)