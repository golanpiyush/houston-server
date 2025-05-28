from flask import Flask, request, jsonify
from flask_pymongo import PyMongo
from bson.objectid import ObjectId

app = Flask(__name__)

# MongoDB configuration
app.config["MONGO_URI"] = "mongodb://defusejadmin:lovesexprogram45@106.219.88.238/myAppDatabasehOUSTON"
mongo = PyMongo(app)

# Define collections
users = mongo.db.users  # Reference to 'users' collection
friends = mongo.db.friends  # Reference to 'friends' collection

# Get all friends for a user
@app.route('/api/friends', methods=['GET'])
def get_friends():
    user_id = request.args.get('user_id')
    user = users.find_one({"_id": ObjectId(user_id)})

    if not user:
        return jsonify({"message": "User not found"}), 404

    friend_ids = user.get("friends", [])
    friend_details = list(users.find({"_id": {"$in": friend_ids}},
                                      {"_id": 1, "username": 1, "profile_picture": 1, "current_song": 1}))
    return jsonify(friend_details), 200

@app.route('/test-connection', methods=['GET'])
def test_connection():
    try:
        db_status = mongo.cx.server_info()  # Check MongoDB server status
        return jsonify({"message": "Connected to MongoDB", "serverInfo": db_status}), 200
    except Exception as e:
        return jsonify({"message": "Connection failed", "error": str(e)}), 500
# Remove a friend
@app.route('/api/friends/remove', methods=['POST'])
def remove_friend():
    data = request.json
    user_id = data['user_id']
    friend_id = data['friend_id']

    users.update_one({"_id": ObjectId(user_id)}, {"$pull": {"friends": ObjectId(friend_id)}})
    users.update_one({"_id": ObjectId(friend_id)}, {"$pull": {"friends": ObjectId(user_id)}})

    return jsonify({"message": "Friend removed successfully"}), 200

# Get pending friend requests
@app.route('/api/friend-requests', methods=['GET'])
def get_friend_requests():
    user_id = request.args.get('user_id')

    pending_requests = list(friends.find({"receiver_id": int(user_id), "status": "pending"},
                                         {"_id": 1, "sender_id": 1, "status": 1}))
    return jsonify(pending_requests), 200

# Accept a friend request
@app.route('/api/friend-request/accept', methods=['POST'])
def accept_friend_request():
    data = request.json
    sender_id = data['sender_id']
    receiver_id = data['receiver_id']

    friend_request = friends.find_one_and_update(
        {"sender_id": int(sender_id), "receiver_id": int(receiver_id), "status": "pending"},
        {"$set": {"status": "accepted"}}
    )

    if friend_request:
        users.update_one({"_id": ObjectId(sender_id)}, {"$addToSet": {"friends": ObjectId(receiver_id)}})
        users.update_one({"_id": ObjectId(receiver_id)}, {"$addToSet": {"friends": ObjectId(sender_id)}})
        return jsonify({"message": "Friend request accepted"}), 200

    return jsonify({"message": "Friend request not found"}), 404

if __name__ == '__main__':
    app.run(debug=True)
