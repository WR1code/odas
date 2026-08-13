package com.example.avtwinresponder

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.os.SystemClock
import android.text.Editable
import android.text.InputType
import android.text.TextWatcher
import android.view.Gravity
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast

class MainActivity : Activity() {
    companion object {
        private const val REQ_AUDIO = 1001
        private const val REQ_C1_FILE = 2001
        private const val REQ_C2_FILE = 2002
        private const val REQ_RESULT_TREE = 3001
        private const val PREFS = "avtwin_session_prefs"
        private const val PREF_C1_URI = "c1_uri"
        private const val PREF_C2_URI = "c2_uri"
        private const val PREF_TREE_URI = "result_tree_uri"
        private const val PREF_LINUX_IP = "linux_ip"
        private const val PREF_CONTROL_PORT = "control_port"
        private const val PREF_RESULT_PORT = "result_port"
        private const val PREF_DEBUG_AUDIO = "debug_audio"
        private const val PREF_POSE_X = "manual_pose_x_m"
        private const val PREF_POSE_Y = "manual_pose_y_m"
        private const val PREF_POSE_Z = "manual_pose_z_m"
        private const val PREF_POSE_YAW = "manual_pose_yaw_deg"
        private const val PREF_POSE_PITCH = "manual_pose_pitch_deg"
        private const val PREF_POSE_ROLL = "manual_pose_roll_deg"
        private const val PREF_POSE_REVISION = "manual_pose_revision"
    }

    private lateinit var c1Info: TextView
    private lateinit var c2Info: TextView
    private lateinit var networkInfo: TextView
    private lateinit var folderInfo: TextView
    private lateinit var status: TextView
    private lateinit var metrics: TextView
    private lateinit var host: EditText
    private lateinit var controlPort: EditText
    private lateinit var resultPort: EditText
    private lateinit var debugAudio: CheckBox
    private lateinit var selectC1: Button
    private lateinit var selectC2: Button
    private lateinit var defaultC1: Button
    private lateinit var defaultC2: Button
    private lateinit var selectFolder: Button
    private lateinit var startSession: Button
    private lateinit var pauseResume: Button
    private lateinit var stopSession: Button
    private lateinit var udpTest: Button
    private lateinit var c2Test: Button
    private lateinit var poseX: EditText
    private lateinit var poseY: EditText
    private lateinit var poseZ: EditText
    private lateinit var poseYaw: EditText
    private lateinit var posePitch: EditText
    private lateinit var poseRoll: EditText
    private lateinit var applyPose: Button
    private lateinit var poseStatus: TextView

    private var c1Signal: ProbeSignal = ProbeDefaults.c1()
    private var c2Signal: ProbeSignal = ProbeDefaults.c2()
    private var resultTreeUri: Uri? = null
    private var responder: AcousticResponder? = null
    private var paused = false
    private var poseRevision = 0L
    @Volatile private var currentPose: ManualPose = ManualPose.origin()
    private val logBuffer = StringBuilder()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        buildUi()
        ensureAudioPermission()
        restoreSettings()
        updateProbeInfo()
        updateNetworkInfo()
        updateFolderInfo()
        showIdleMetrics()
    }

    override fun onResume() {
        super.onResume()
        if (::networkInfo.isInitialized) updateNetworkInfo()
    }

    private fun buildUi() {
        val density = resources.displayMetrics.density
        fun dp(x: Int) = (x * density).toInt()
        fun sectionTitle(textValue: String) = TextView(this).apply {
            text = textValue
            textSize = 16f
            setPadding(0, dp(12), 0, dp(4))
        }
        fun fieldLabel(textValue: String) = TextView(this).apply {
            text = textValue
            textSize = 13f
        }
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(14), dp(18), dp(14))
        }
        root.addView(TextView(this).apply {
            text = "AV-Twin Continuous Acoustic Responder v0.9.0 ACK+POSE"
            textSize = 21f
            gravity = Gravity.CENTER_HORIZONTAL
        })
        root.addView(TextView(this).apply {
            text = "STRICT ARM 强制开启：1 个有效 ARM 最多触发 1 次 C2。网络只授权轮次，不参与 t2/t3 声学计时。"
            textSize = 13f
            setPadding(0, dp(5), 0, dp(8))
        })

        c1Info = TextView(this).apply { textSize = 12f; setTextIsSelectable(true) }
        root.addView(c1Info)
        val c1Row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        selectC1 = Button(this).apply { text = "SELECT C1 WAV"; setOnClickListener { chooseProbeFile(REQ_C1_FILE) } }
        defaultC1 = Button(this).apply { text = "DEFAULT C1"; setOnClickListener { setDefaultProbe(true) } }
        c1Row.addView(selectC1, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        c1Row.addView(defaultC1, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        root.addView(c1Row)

        c2Info = TextView(this).apply { textSize = 12f; setTextIsSelectable(true); setPadding(0, dp(3), 0, 0) }
        root.addView(c2Info)
        val c2Row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        selectC2 = Button(this).apply { text = "SELECT C2 WAV"; setOnClickListener { chooseProbeFile(REQ_C2_FILE) } }
        defaultC2 = Button(this).apply { text = "DEFAULT C2"; setOnClickListener { setDefaultProbe(false) } }
        c2Row.addView(selectC2, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        c2Row.addView(defaultC2, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        root.addView(c2Row)

        root.addView(sectionTitle("1. 当前坐标与朝向（每次响应自动保存）"))
        root.addView(TextView(this).apply {
            text = "单位：位置为米，朝向为角度。采用 ZYX yaw/pitch/roll；修改后点击“应用”，下一次有效 C1 → C2 会保存当时已应用的位姿。会话运行中也可以更新，以便连续采集多个位置。"
            textSize = 12f
            setPadding(0, 0, 0, dp(4))
        })
        fun poseField(label: String): Pair<LinearLayout, EditText> {
            val field = EditText(this).apply {
                hint = "0"
                inputType = InputType.TYPE_CLASS_NUMBER or
                    InputType.TYPE_NUMBER_FLAG_DECIMAL or InputType.TYPE_NUMBER_FLAG_SIGNED
                isSingleLine = true
            }
            val box = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(3), 0, dp(3), 0)
                addView(fieldLabel(label))
                addView(field)
            }
            return box to field
        }
        val positionRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        val (xBox, xField) = poseField("X（米）")
        val (yBox, yField) = poseField("Y（米）")
        val (zBox, zField) = poseField("Z（米）")
        poseX = xField
        poseY = yField
        poseZ = zField
        positionRow.addView(xBox, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        positionRow.addView(yBox, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        positionRow.addView(zBox, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        root.addView(positionRow)
        val orientationRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        val (yawBox, yawField) = poseField("Yaw 偏航°")
        val (pitchBox, pitchField) = poseField("Pitch 俯仰°")
        val (rollBox, rollField) = poseField("Roll 横滚°")
        poseYaw = yawField
        posePitch = pitchField
        poseRoll = rollField
        orientationRow.addView(yawBox, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        orientationRow.addView(pitchBox, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        orientationRow.addView(rollBox, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        root.addView(orientationRow)
        applyPose = Button(this).apply {
            text = "应用当前位姿（下一次 C1 使用）"
            setOnClickListener { applyManualPose(showToast = true) }
        }
        root.addView(applyPose)
        poseStatus = TextView(this).apply {
            textSize = 12f
            setTextIsSelectable(true)
            text = "当前位姿尚未应用"
        }
        root.addView(poseStatus)

        root.addView(sectionTitle("2. 安卓端监听地址（Linux → 安卓）"))
        networkInfo = TextView(this).apply {
            textSize = 12f
            setTextIsSelectable(true)
            setPadding(0, dp(7), 0, dp(3))
        }
        root.addView(networkInfo)

        root.addView(sectionTitle("3. Linux 电脑地址（安卓 → Linux）"))
        root.addView(TextView(this).apply {
            text = "填写运行 AV-Twin Linux 程序的电脑 Wi-Fi IPv4。它既是安卓回传结果的目标，也是唯一允许发送 ARM 的电脑。"
            textSize = 12f
            setPadding(0, 0, 0, dp(3))
        })
        root.addView(fieldLabel("Linux 电脑 Wi-Fi IPv4（不是安卓 IP）"))
        host = EditText(this).apply {
            hint = "例如 192.168.1.100"
            inputType = InputType.TYPE_CLASS_TEXT
            isSingleLine = true
        }
        root.addView(host)
        val portRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        val androidPortBox = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, 0, dp(6), 0)
        }
        androidPortBox.addView(fieldLabel("安卓 ARM 监听端口"))
        controlPort = EditText(this).apply {
            hint = "默认 5006"
            inputType = InputType.TYPE_CLASS_NUMBER
            isSingleLine = true
        }
        androidPortBox.addView(controlPort)
        val linuxPortBox = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(6), 0, 0, 0)
        }
        linuxPortBox.addView(fieldLabel("Linux 结果接收端口"))
        resultPort = EditText(this).apply {
            hint = "默认 5005"
            inputType = InputType.TYPE_CLASS_NUMBER
            isSingleLine = true
        }
        linuxPortBox.addView(resultPort)
        portRow.addView(androidPortBox, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        portRow.addView(linuxPortBox, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        root.addView(portRow)

        root.addView(TextView(this).apply {
            text = "端口方向：Linux 把 ARM 发到上方“安卓 ARM 监听端口”；安卓把测试和测量结果发到“Linux 结果接收端口”。"
            textSize = 12f
            setPadding(0, dp(2), 0, dp(4))
        })

        folderInfo = TextView(this).apply { textSize = 12f; setTextIsSelectable(true); setPadding(0, dp(5), 0, 0) }
        root.addView(folderInfo)
        selectFolder = Button(this).apply { text = "选择结果保存目录"; setOnClickListener { chooseResultFolder() } }
        root.addView(selectFolder)
        debugAudio = CheckBox(this).apply { text = "保存调试音频（默认关闭）" }
        root.addView(debugAudio)

        startSession = Button(this).apply { text = "开始 STRICT ARM 会话"; setOnClickListener { startContinuousSession() } }
        root.addView(startSession)
        val sessionRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        pauseResume = Button(this).apply { text = "暂停监听"; isEnabled = false; setOnClickListener { togglePause() } }
        stopSession = Button(this).apply { text = "安全停止并保存"; isEnabled = false; setOnClickListener { safeStop() } }
        sessionRow.addView(pauseResume, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        sessionRow.addView(stopSession, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        root.addView(sessionRow)

        root.addView(sectionTitle("4. Wi-Fi / UDP 连通测试"))
        root.addView(TextView(this).apply {
            text = "先让 Linux 端监听结果端口，再点击下面按钮。Android 会发送带随机 nonce 的 ping；只有收到 Linux 的匹配 reply 才显示 PASS，因此同时验证去程和回程，不播放声音。Linux 也可以向本机控制端口发起同一检验。"
            textSize = 12f
            setPadding(0, 0, 0, dp(3))
        })
        val testRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        udpTest = Button(this).apply { text = "UDP 双向检验"; setOnClickListener { runUdpTest() } }
        c2Test = Button(this).apply { text = "TEST C2 x20"; setOnClickListener { runC2Test() } }
        testRow.addView(udpTest, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        testRow.addView(c2Test, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        root.addView(testRow)

        metrics = TextView(this).apply { textSize = 13f; setTextIsSelectable(true); setPadding(0, dp(6), 0, dp(4)) }
        root.addView(metrics)
        status = TextView(this).apply { textSize = 12f; setTextIsSelectable(true) }
        root.addView(status, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))

        // Scroll the whole screen instead of only the log. This keeps every control reachable
        // on short landscape displays and when Android increases the font/display size.
        val pageScroll = ScrollView(this).apply {
            isFillViewport = true
            addView(
                root,
                ScrollView.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT
                )
            )
        }
        setContentView(pageScroll)

        controlPort.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) = Unit
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {
                updateNetworkInfo()
            }
            override fun afterTextChanged(s: Editable?) = Unit
        })
    }

    private fun startContinuousSession() {
        if (!ensurePermissionForAction()) return
        if (responder?.isRunning() == true || responder?.isTestRunning() == true) return
        if (!applyManualPose(showToast = false)) return
        val tree = resultTreeUri
        if (tree == null) {
            Toast.makeText(this, "请先选择结果保存目录", Toast.LENGTH_LONG).show()
            return
        }
        val validation = SafSessionStorage.validateTree(this, tree)
        if (!validation.first) {
            Toast.makeText(this, "保存目录不可用：${validation.second}，请重新授权", Toast.LENGTH_LONG).show()
            return
        }
        val ip = host.text.toString().trim()
        val cp = controlPort.text.toString().toIntOrNull() ?: 5006
        val rp = resultPort.text.toString().toIntOrNull() ?: 5005
        if (ip.isBlank() || cp !in 1..65535 || rp !in 1..65535) {
            Toast.makeText(this, "检查 Linux IP / 端口", Toast.LENGTH_LONG).show()
            return
        }

        StrictArmNetworkPolicy.setExpectedLinuxHost(ip)
        saveSettings()
        updateNetworkInfo()
        responder = makeResponder()
        setSessionControls(true)
        paused = false
        pauseResume.text = "暂停监听"
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        appendLog("STRICT ARM ON: expected Linux source=$ip; Android listens ARM on 0.0.0.0:$cp")
        appendLog("IMPORTANT: Linux must send ARM to Android Wi-Fi IP shown above, then send C1. Network timestamps are never used as t2/t3.")
        responder!!.startSession(
            AcousticResponder.SessionConfig(
                linuxHost = ip,
                controlPort = cp,
                resultPort = rp,
                resultTreeUri = tree,
                saveDebugAudio = debugAudio.isChecked
            )
        )
    }

    private fun togglePause() {
        val r = responder ?: return
        if (!r.isRunning()) return
        if (!paused) {
            r.pauseListening()
            paused = true
            pauseResume.text = "继续监听"
        } else {
            r.resumeListening()
            paused = false
            pauseResume.text = "暂停监听"
        }
    }

    private fun safeStop() {
        responder?.stopAndSave()
        appendLog("Safe stop requested; waiting for session finalization")
    }

    private fun runUdpTest() {
        val ip = host.text.toString().trim()
        val rp = resultPort.text.toString().toIntOrNull() ?: 5005
        if (ip.isBlank() || rp !in 1..65535) {
            Toast.makeText(this, "请先填写有效的 Linux Wi-Fi IPv4 和结果端口", Toast.LENGTH_LONG).show()
            return
        }
        val androidIp = LocalNetworkInfo.preferredLanIpv4() ?: "unavailable"
        appendLog("UDP 双向检验：Android $androidIp → Linux $ip:$rp → Android（不播放声音）")
        if (responder?.isRunning() == true) responder?.testUdp(ip, rp) else {
            responder = makeResponder()
            responder!!.testUdp(ip, rp)
        }
    }

    private fun runC2Test() {
        if (!ensurePermissionForAction()) return
        if (responder?.isRunning() == true || responder?.isTestRunning() == true) return
        responder = makeResponder()
        setProbeControls(false)
        c2Test.isEnabled = false
        responder!!.startRepeatedPlaybackTest()
    }

    private fun makeResponder(): AcousticResponder = AcousticResponder(
        context = this,
        c1Signal = c1Signal,
        c2Signal = c2Signal,
        currentManualPose = { currentPose },
        onStatus = { message ->
            runOnUiThread {
                appendLog(message)
                if (responder?.isTestRunning() != true && responder?.isRunning() != true) setProbeControls(true)
            }
        },
        onSnapshot = { snap -> runOnUiThread { updateSnapshot(snap) } }
    )

    private fun updateSnapshot(s: AcousticResponder.SessionSnapshot) {
        metrics.text =
            "STRICT ARM=ON | state=${s.state}\n" +
                "Android本地保存session=${s.sessionId ?: "--"}\n" +
                "已绑定Linux session=${s.pairedLinuxSessionId ?: "--（等待首个ARM）"}\n" +
                "measurement_id=${s.measurementId ?: "--"} | pending ARM=${s.pendingArmMeasurementId ?: "--"}\n" +
                "成功响应=${s.successResponses} | C1未通过=${s.c1Rejected} | C2失败=${s.c2Failures} | UDP失败=${s.udpFailures}\n" +
                "last reply_delay_samples=${s.lastReplyDelaySamples ?: "--"} | t3_precise=${s.lastT3Precise}\n" +
                "last saved pose=${s.lastSavedPose?.summary() ?: "--"}\n" +
                "input=${s.inputRoute}\noutput=${s.outputRoute}\n" +
                "note=${s.note}"
        if (s.state == "STOPPED") {
            setSessionControls(false)
            setProbeControls(true)
            window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            paused = false
            pauseResume.text = "暂停监听"
        }
    }

    private fun showIdleMetrics() {
        metrics.text = "STRICT ARM=ON | state=STOPPED\nAndroid本地保存session=--\n已绑定Linux session=--\nmeasurement_id=--\n成功响应=0 | C1未通过=0 | C2失败=0 | UDP失败=0\nt3_precise=false"
    }

    private fun updateNetworkInfo() {
        val cp = if (::controlPort.isInitialized) controlPort.text.toString().toIntOrNull() ?: 5006 else 5006
        val preferred = LocalNetworkInfo.preferredLanIpv4() ?: "unavailable"
        val linuxArmTarget = if (preferred == "unavailable") {
            "暂不可用（请先让安卓连接 Wi-Fi）"
        } else {
            "$preferred:$cp"
        }
        networkInfo.text =
            "安卓 Wi-Fi IPv4：$preferred\n" +
                "安卓 ARM 监听端口：$cp\n" +
                "Linux 端发送 ARM 的目标：$linuxArmTarget\n" +
                "本机网络接口：${LocalNetworkInfo.display()}\n" +
                "这里仅用于查看；下面的输入框才是 Linux 电脑地址。"
    }

    private fun chooseProbeFile(requestCode: Int) {
        if (responder?.isRunning() == true) return
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "audio/*"
            putExtra(Intent.EXTRA_MIME_TYPES, arrayOf("audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave", "application/octet-stream"))
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION)
        }
        startActivityForResult(intent, requestCode)
    }

    private fun chooseResultFolder() {
        if (responder?.isRunning() == true) return
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE).apply {
            addFlags(
                Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION or
                    Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION or Intent.FLAG_GRANT_PREFIX_URI_PERMISSION
            )
        }
        startActivityForResult(intent, REQ_RESULT_TREE)
    }

    @Deprecated("Kept for the minimal Activity implementation")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (resultCode != RESULT_OK) return
        val uri = data?.data ?: return
        try {
            if (requestCode == REQ_RESULT_TREE) {
                val flags = data.flags and (Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
                contentResolver.takePersistableUriPermission(uri, flags)
                resultTreeUri = uri
                getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(PREF_TREE_URI, uri.toString()).apply()
                updateFolderInfo()
                return
            }
            if (requestCode != REQ_C1_FILE && requestCode != REQ_C2_FILE) return
            try { contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) } catch (_: Throwable) {}
            val loaded = WavProbeLoader.load(this, uri)
            if (requestCode == REQ_C1_FILE) {
                c1Signal = loaded
                getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(PREF_C1_URI, uri.toString()).apply()
                appendLog("C1 loaded: ${loaded.summary()} SHA256=${loaded.sourceSha256}")
            } else {
                c2Signal = loaded
                getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(PREF_C2_URI, uri.toString()).apply()
                appendLog("C2 loaded: ${loaded.summary()} SHA256=${loaded.sourceSha256}")
            }
            updateProbeInfo()
        } catch (t: Throwable) {
            appendLog("FILE/DIRECTORY ERROR: ${t.javaClass.simpleName}: ${t.message}")
            Toast.makeText(this, t.message ?: "选择失败", Toast.LENGTH_LONG).show()
        }
    }

    private fun setDefaultProbe(c1: Boolean) {
        if (c1) {
            c1Signal = ProbeDefaults.c1()
            getSharedPreferences(PREFS, MODE_PRIVATE).edit().remove(PREF_C1_URI).apply()
        } else {
            c2Signal = ProbeDefaults.c2()
            getSharedPreferences(PREFS, MODE_PRIVATE).edit().remove(PREF_C2_URI).apply()
        }
        updateProbeInfo()
    }

    private fun restoreSettings() {
        val p = getSharedPreferences(PREFS, MODE_PRIVATE)
        host.setText(p.getString(PREF_LINUX_IP, "192.168.1.100"))
        controlPort.setText(p.getInt(PREF_CONTROL_PORT, 5006).toString())
        resultPort.setText(p.getInt(PREF_RESULT_PORT, 5005).toString())
        debugAudio.isChecked = p.getBoolean(PREF_DEBUG_AUDIO, false)
        poseX.setText(p.getString(PREF_POSE_X, "0"))
        poseY.setText(p.getString(PREF_POSE_Y, "0"))
        poseZ.setText(p.getString(PREF_POSE_Z, "0"))
        poseYaw.setText(p.getString(PREF_POSE_YAW, "0"))
        posePitch.setText(p.getString(PREF_POSE_PITCH, "0"))
        poseRoll.setText(p.getString(PREF_POSE_ROLL, "0"))
        poseRevision = p.getLong(PREF_POSE_REVISION, 0L)
        if (!applyManualPose(showToast = false)) {
            poseX.setText("0")
            poseY.setText("0")
            poseZ.setText("0")
            poseYaw.setText("0")
            posePitch.setText("0")
            poseRoll.setText("0")
            applyManualPose(showToast = false)
        }
        p.getString(PREF_TREE_URI, null)?.let { resultTreeUri = Uri.parse(it) }
        restoreProbe(p.getString(PREF_C1_URI, null), true)
        restoreProbe(p.getString(PREF_C2_URI, null), false)
    }

    private fun restoreProbe(uriString: String?, c1: Boolean) {
        if (uriString.isNullOrBlank()) return
        try {
            val loaded = WavProbeLoader.load(this, Uri.parse(uriString))
            if (c1) c1Signal = loaded else c2Signal = loaded
        } catch (_: Throwable) {
            getSharedPreferences(PREFS, MODE_PRIVATE).edit().remove(if (c1) PREF_C1_URI else PREF_C2_URI).apply()
        }
    }

    private fun saveSettings() {
        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
            .putString(PREF_LINUX_IP, host.text.toString().trim())
            .putInt(PREF_CONTROL_PORT, controlPort.text.toString().toIntOrNull() ?: 5006)
            .putInt(PREF_RESULT_PORT, resultPort.text.toString().toIntOrNull() ?: 5005)
            .putBoolean(PREF_DEBUG_AUDIO, debugAudio.isChecked)
            .apply()
    }

    private fun applyManualPose(showToast: Boolean): Boolean {
        fun value(field: EditText): Double? =
            field.text.toString().trim().toDoubleOrNull()?.takeIf { it.isFinite() }

        val x = value(poseX)
        val y = value(poseY)
        val z = value(poseZ)
        val yaw = value(poseYaw)
        val pitch = value(posePitch)
        val roll = value(poseRoll)
        if (listOf(x, y, z, yaw, pitch, roll).any { it == null }) {
            poseStatus.text = "位姿输入无效：六个字段都必须是有限数字。当前采集仍使用上一次已应用的位姿。"
            if (showToast) Toast.makeText(this, "请检查坐标和朝向输入", Toast.LENGTH_LONG).show()
            return false
        }

        poseRevision++
        currentPose = ManualPose(
            xMeters = x!!,
            yMeters = y!!,
            zMeters = z!!,
            yawDegrees = yaw!!,
            pitchDegrees = pitch!!,
            rollDegrees = roll!!,
            revision = poseRevision,
            updatedAtDiagnosticMs = SystemClock.elapsedRealtime()
        )
        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
            .putString(PREF_POSE_X, poseX.text.toString().trim())
            .putString(PREF_POSE_Y, poseY.text.toString().trim())
            .putString(PREF_POSE_Z, poseZ.text.toString().trim())
            .putString(PREF_POSE_YAW, poseYaw.text.toString().trim())
            .putString(PREF_POSE_PITCH, posePitch.text.toString().trim())
            .putString(PREF_POSE_ROLL, poseRoll.text.toString().trim())
            .putLong(PREF_POSE_REVISION, poseRevision)
            .apply()
        poseStatus.text = "已应用：${currentPose.summary()}\n可继续修改并应用；后续响应会使用新的 revision。"
        appendLog("Manual pose applied: ${currentPose.summary()}")
        if (showToast) Toast.makeText(this, "当前位姿已应用", Toast.LENGTH_SHORT).show()
        return true
    }

    private fun updateProbeInfo() {
        c1Info.text = "C1: ${c1Signal.summary()}\n${c1Signal.channelDiagnostics()}"
        c2Info.text = "C2: ${c2Signal.summary()}\n${c2Signal.channelDiagnostics()}"
    }

    private fun updateFolderInfo() {
        val uri = resultTreeUri
        if (uri == null) {
            folderInfo.text = "结果保存目录：未选择"
            return
        }
        val validation = SafSessionStorage.validateTree(this, uri)
        val label = SafSessionStorage.displayName(this, uri) ?: "selected tree"
        folderInfo.text = "结果保存目录：$label\n$uri\npermission=${if (validation.first) "OK" else "INVALID: ${validation.second}"}"
    }

    private fun setSessionControls(active: Boolean) {
        startSession.isEnabled = !active
        pauseResume.isEnabled = active
        stopSession.isEnabled = active
        host.isEnabled = !active
        controlPort.isEnabled = !active
        resultPort.isEnabled = !active
        debugAudio.isEnabled = !active
        selectFolder.isEnabled = !active
        udpTest.isEnabled = !active
        c2Test.isEnabled = !active
        setProbeControls(!active)
    }

    private fun setProbeControls(enabled: Boolean) {
        selectC1.isEnabled = enabled
        selectC2.isEnabled = enabled
        defaultC1.isEnabled = enabled
        defaultC2.isEnabled = enabled
        if (responder?.isRunning() != true) c2Test.isEnabled = enabled
    }

    private fun appendLog(message: String) {
        if (message.isBlank()) return
        logBuffer.append(message).append('\n')
        if (logBuffer.length > 24000) logBuffer.delete(0, 6000)
        status.text = logBuffer.toString()
    }

    private fun ensurePermissionForAction(): Boolean {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            ensureAudioPermission()
            return false
        }
        return true
    }

    private fun ensureAudioPermission() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQ_AUDIO)
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_AUDIO && grantResults.firstOrNull() != PackageManager.PERMISSION_GRANTED) {
            Toast.makeText(this, "Microphone permission is required", Toast.LENGTH_LONG).show()
        }
    }

    override fun onDestroy() {
        responder?.stopAndSave()
        super.onDestroy()
    }
}
