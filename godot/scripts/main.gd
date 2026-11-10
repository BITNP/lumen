extends Control
## Presentation process client: connects to the host presentation bridge and
## applies subtitle/speech/expression/motion/PPT commands. Pure display: the
## window takes no user input and sends nothing upstream but the initial
## hello (ADR-0005). The bridge protocol is module-internal (ADR-0002).
## Stage layout and the slide entrance animation follow the hand-designed
## visuals of the retired vtuber-frontend (1920x1080 canvas_items stretch).

const CONFIG_PATH := "res://lumen-config.json"
const RECONNECT_INTERVAL := 1.0
const SLIDE_WIDTH := 1280.0  # fixed display width; height follows the PNG aspect

var _socket := WebSocketPeer.new()
var _url := "ws://127.0.0.1:8790/"
var _connected := false
var _reconnect_timer := 0.0

@onready var _label: Label = $CaptionLayer/TextLabel/Label
@onready var _avatar = $Avatar/Model
@onready var _slide: TextureRect = $Slide
@onready var _animations: AnimationPlayer = $AnimationPlayer


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
		push_warning("lumen bridge connect failed: %s" % error_string(err))


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
			push_warning("lumen bridge disconnected")
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
			_label.text = text.substr(0, visible)
		"subtitle_clear":
			_label.text = ""
		"speech_state":
			if _avatar != null:
				_avatar.set_speaking(bool(message.get("active", false)))
		"emotion":
			if _avatar != null:
				_avatar.play_expression(String(message.get("tag", "")))
		"motion":
			if _avatar != null:
				_avatar.play_motion(String(message.get("tag", "")))
		"ppt":
			_apply_slide(message)
		_:
			pass


func _apply_slide(message: Dictionary) -> void:
	if String(message.get("action", "")) == "hide":
		if _slide.visible:
			if _animations.has_animation("unload_slide"):
				_animations.play("unload_slide")
			else:
				_slide.visible = false
		return
	var path := String(message.get("image", ""))
	var image := Image.load_from_file(path)
	if image == null:
		push_warning("slide image unavailable: %s" % path)
		return
	_slide.texture = ImageTexture.create_from_image(image)
	# Aspect-true display: the rect keeps the design width and takes the height
	# from the PNG ratio, so the scale-to-fill draw never distorts.  The node is
	# top-left anchored, so the box grows downward only and the top-left corner
	# (and with it the whole top edge) stays put across pages.
	_slide.size = Vector2(SLIDE_WIDTH, SLIDE_WIDTH * image.get_height() / image.get_width())
	if not _slide.visible:
		_slide.visible = true
		if _animations.has_animation("load_slide"):
			_animations.play("load_slide")  # slide in, avatar to the lower-right corner
