extends GDCubismUserModel
## Thin Live2D driver for the presentation process (gd_cubism 0.9).
## Idle layer (breath/head/blink) + expression clips + named motions; the
## mouth is driven only by the host speech flag (linear-reveal design: no
## visemes). Never touches the network; commands arrive via the bridge in
## main.gd.

const MODEL := "res://assets/raspberry_girl.model3.json"
const EXPRESSIONS := ["nod", "shake_head", "wink"]
const MOTIONS := ["wave", "point", "sweat", "no"]
const RESET_MOTION := "reset"
const BLINK_PERIOD := 4.0
const BLINK_SECONDS := 0.32
const RECOVERY_SECONDS := 0.25

# face_expressions/*.json record these mocap fields; the triples are
# (scale, bias) onto this model's parameters, tuned for this asset set.
const FACE_MAP := [
	["ParamEyeLOpen", -2.0, 1.0],
	["ParamEyeROpen", -2.0, 1.0],
	["ParamMouthForm", 1.0 / 0.11, -3.45],
	["ParamAngleY", 2.0, 0.0],
	["ParamAngleX", -1.0, 0.0],
	["ParamAngleZ", 1.0, 0.0],
	["ParamBrowLY", 2.0, -1.0],
	["ParamBrowRY", 2.0, -1.0],
	["ParamEyeBallX", 1.5, 0.0],
	["ParamEyeBallY", 3.0, 0.0],
]

var _parameters: Dictionary = {}
var _clips: Dictionary = {}
var _speaking := false
var _expression := ""
var _expression_time := 0.0
var _expression_recovery := 0.0
var _recovery_face: Dictionary = {}
var _face: Dictionary = {}
var _time := 0.0
# Motion tail recovery: the bundled motion3.json files end on the gesture peak
# (sweat ends with ParamSweat=1 and a tilted head) and Cubism keeps restoring
# that ending forever: GDCubism re-loads its parameter backup right before
# every motion update, so only values written *by a motion* survive into the
# next frame. GDScript writes (cubism_process runs after that backup window)
# are therefore re-applied every frame and vanish the moment they stop. The
# fix is baked as data: reset.motion3.json holds every curve the gestures
# touch at its neutral value (parameter default / loaded part opacity) and
# fades in over 0.5s, so Cubism itself blends the pose back and persists it.
var _motion_had_entries := false
var _gesture_pending := false


func _ready() -> void:
	assets = MODEL
	for parameter: GDCubismParameter in get_parameters():
		_parameters[parameter.id] = parameter
	for name: String in EXPRESSIONS:
		var clip: Variant = JSON.parse_string(FileAccess.get_file_as_string(
			"res://assets/face_expressions/%s.json" % name))
		if clip is Dictionary and not (clip as Dictionary).get("frames", []).is_empty():
			_clips[name] = clip
	# The addon effects would fight the expression layer over eye parameters.
	for child: Node in get_children():
		if child is GDCubismEffectEyeBlink or child is GDCubismEffectBreath:
			child.active = false
	var effect := GDCubismEffectCustom.new()
	add_child(effect)
	effect.cubism_process.connect(_advance)


func set_speaking(active: bool) -> void:
	_speaking = active


func play_expression(tag: String) -> bool:
	if not _clips.has(tag):
		push_warning("unknown expression tag: %s" % tag)
		return false
	_expression = tag
	_expression_time = 0.0
	_expression_recovery = 0.0
	return true


func play_motion(tag: String) -> bool:
	if not MOTIONS.has(tag):
		push_warning("unknown motion tag: %s" % tag)
		return false
	var handle: GDCubismMotionQueueEntryHandle = start_motion_loop(tag, 0, 3, false, true)
	if handle != null and handle.get_error() == OK:
		_gesture_pending = true  # queue drain later starts the reset motion
		return true
	return false


func _write(id: String, value: float) -> void:
	if not _parameters.has(id):
		return
	var parameter: GDCubismParameter = _parameters[id]
	parameter.value = clampf(value, parameter.minimum_value, parameter.maximum_value)


func _advance(_model: GDCubismUserModel, delta: float) -> void:
	_time += delta
	var queue_empty := get_cubism_motion_queue_entries().is_empty()
	if not queue_empty:
		_motion_had_entries = true
	elif _motion_had_entries:
		_motion_had_entries = false
		if _gesture_pending:
			_gesture_pending = false
			# Lower priority than the gestures so a new one can cut it short.
			start_motion_loop(RESET_MOTION, 0, 2, false, true)
	_write("ParamBreath", 0.5 + 0.5 * sin(_time * TAU / 3.0))
	if queue_empty:
		_write("ParamAngleX", sin(_time * TAU / 3.0) * 5.0)
		_write("ParamAngleZ", cos(_time * TAU / 3.0) * 3.0)
	_advance_expression(delta)
	_advance_blink()
	# Mouth follows the host playback flag only (no word-level timestamps).
	_write("ParamMouthOpenY", 0.25 + 0.65 * absf(sin(_time * 14.0)) if _speaking else 0.0)


func _advance_expression(delta: float) -> void:
	if not _expression.is_empty():
		var clip: Dictionary = _clips[_expression]
		var frames: Array = clip["frames"]
		var index := int(round(_expression_time * float(clip["fps"])))
		if index >= frames.size():
			_expression = ""
			_expression_recovery = RECOVERY_SECONDS
			_recovery_face = _face.duplicate()
		else:
			_apply_frame(frames[index], delta)
			_expression_time += delta
	if _expression.is_empty() and _expression_recovery > 0.0:
		_expression_recovery = maxf(0.0, _expression_recovery - delta)
		var weight := smoothstep(0.0, RECOVERY_SECONDS, _expression_recovery)
		for mapping: Array in FACE_MAP:
			var id := str(mapping[0])
			var target := float((_parameters[id] as GDCubismParameter).default_value)
			_face[id] = lerpf(target, float(_recovery_face.get(id, target)), weight)
	for id: String in _face:
		_write(id, float(_face[id]))
	if _expression.is_empty() and _expression_recovery == 0.0:
		_face.clear()


func _apply_frame(frame: Array, delta: float) -> void:
	var weight := 1.0 - pow(0.8, delta * 60.0)
	for index in FACE_MAP.size():
		var mapping: Array = FACE_MAP[index]
		var id := str(mapping[0])
		var parameter: GDCubismParameter = _parameters.get(id, null)
		if parameter == null:
			continue
		var target := float(parameter.value)
		if index < frame.size() and frame[index] != null:
			target = float(frame[index]) * float(mapping[1]) + float(mapping[2])
		_face[id] = lerpf(float(_face.get(id, parameter.value)), target, weight)


func _advance_blink() -> void:
	# Deterministic blink cycle: the first BLINK_SECONDS of every period.
	if not _expression.is_empty() or _expression_recovery > 0.0:
		return
	var phase := fposmod(_time, BLINK_PERIOD)
	if phase >= BLINK_SECONDS:
		return
	var openness := 1.0 - sin(phase / BLINK_SECONDS * PI)
	_write("ParamEyeLOpen", openness)
	_write("ParamEyeROpen", openness)
