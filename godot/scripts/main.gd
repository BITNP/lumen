extends Control
## Presentation process client: connects to the host presentation bridge,
## applies subtitle/emotion/motion commands, reports user interactions.
## The bridge protocol is module-internal (ADR-0002); the avatar and PPT
## nodes arrive with M2/M3.

const CONFIG_PATH := "res://presenter-config.json"
const RECONNECT_INTERVAL := 1.0

var _socket := WebSocketPeer.new()
var _url := "ws://127.0.0.1:8790/"
var _connected := false
var _reconnect_timer := 0.0
var _emotion := ""
var _motion := ""

@onready var _subtitle_label: Label = $SubtitleLabel


func _ready() -> void:
	_load_config()
	_connect()


func _load_config() -> void:
	if not FileAccess.file_exists(CONFIG_PATH):
		return
	var file := FileAccess.open(CONFIG_PATH, FileAccess.READ)
	var parsed: Variant = JSON.parse_string(file.get_as_text())
	if typeof(parsed) == TYPE_DICTIONARY and parsed.has("bridge_url"):
		_url = String(parsed["bridge_url"])


func _connect() -> void:
	var err := _socket.connect_to_url(_url)
	if err != OK:
		push_warning("presenter bridge connect failed: %s" % error_string(err))


func _process(delta: float) -> void:
	_socket.poll()
	var state := _socket.get_ready_state()
	if state == WebSocketPeer.STATE_OPEN:
		if not _connected:
			_connected = true
			_socket.send_text(JSON.stringify({"type": "hello"}))
		while _socket.get_available_packet_count() > 0:
			_handle_message(_socket.get_packet().get_string_from_utf8())
	elif state == WebSocketPeer.STATE_CLOSED:
		if _connected:
			push_warning("presenter bridge disconnected")
		_connected = false
		_reconnect_timer += delta
		if _reconnect_timer >= RECONNECT_INTERVAL:
			_reconnect_timer = 0.0
			_socket = WebSocketPeer.new()
			_connect()


func _handle_message(raw: String) -> void:
	var parsed: Variant = JSON.parse_string(raw)
	if typeof(parsed) != TYPE_DICTIONARY:
		return
	var message: Dictionary = parsed
	match String(message.get("type", "")):
		"subtitle_set":
			var text := String(message.get("text", ""))
			var visible := int(message.get("visible_chars", 0))
			_subtitle_label.text = text.substr(0, visible)
		"subtitle_clear":
			_subtitle_label.text = ""
		"emotion":
			_emotion = String(message.get("tag", ""))  # drives Live2D in M2
		"motion":
			_motion = String(message.get("tag", ""))  # drives Live2D in M2
		_:
			pass


func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventMouseButton and event.pressed and event.button_index == MOUSE_BUTTON_LEFT:
		_report_user_event("click", "window")


func _report_user_event(kind: String, detail: String) -> void:
	if not _connected:
		return
	_socket.send_text(JSON.stringify({
		"type": "user_event",
		"kind": kind,
		"detail": detail,
	}))
