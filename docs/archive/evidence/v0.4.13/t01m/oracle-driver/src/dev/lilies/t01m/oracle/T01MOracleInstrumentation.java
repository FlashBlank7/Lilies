package dev.lilies.t01m.oracle;

import android.accessibilityservice.AccessibilityService;
import android.app.Activity;
import android.app.Instrumentation;
import android.app.UiAutomation;
import android.graphics.Bitmap;
import android.graphics.Rect;
import android.graphics.RectF;
import android.hardware.display.DisplayManager;
import android.os.Bundle;
import android.os.Parcelable;
import android.os.ParcelFileDescriptor;
import android.os.SystemClock;
import android.util.Base64;
import android.view.Display;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.Future;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Generic external black-box driver frozen before the target application exists.
 *
 * <p>The driver only observes Android accessibility semantics and pixels. It never reads the
 * target package's files, source repository, implementation classes, or private data.</p>
 */
public final class T01MOracleInstrumentation extends Instrumentation {
    private static final int MAX_TREE_NODES = 4_000;
    private static final int MAX_TREE_DEPTH = 80;
    private static final int MAX_STRING_CHARS = 8_192;
    private static final long DEFAULT_TIMEOUT_MS = 8_000L;
    private static final String TARGET_PACKAGE = "dev.lilies.civilizationseed";

    private Bundle arguments;
    private UiAutomation automation;

    @Override
    public void onCreate(Bundle input) {
        super.onCreate(input);
        arguments = input == null ? new Bundle() : new Bundle(input);
        start();
    }

    @Override
    public void onStart() {
        Bundle result = new Bundle();
        try {
            automation = getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
            automation.setRotation(UiAutomation.ROTATION_UNFREEZE);
            configureUiAutomation();
            String action = requireArgument("action");
            result.putString("action", action);
            execute(action, result);
            result.putString("status", "pass");
            finish(Activity.RESULT_OK, result);
        } catch (Throwable error) {
            result.putString("status", "fail");
            result.putString("error_type", error.getClass().getName());
            result.putString("error", safeUtf8String(error.getMessage()));
            finish(Activity.RESULT_CANCELED, result);
        }
    }

    private void configureUiAutomation() {
        android.accessibilityservice.AccessibilityServiceInfo info = automation.getServiceInfo();
        info.flags |= android.accessibilityservice.AccessibilityServiceInfo
                .FLAG_RETRIEVE_INTERACTIVE_WINDOWS;
        info.flags |= android.accessibilityservice.AccessibilityServiceInfo
                .FLAG_REPORT_VIEW_IDS;
        automation.setServiceInfo(info);
    }

    private void execute(String action, Bundle result) throws Exception {
        switch (action) {
            case "wait":
                withUniqueNode(node -> describeNode(node, result));
                return;
            case "click":
                withUniqueNode(node -> {
                    if (!performOnSelfOrClickableAncestor(
                            node,
                            AccessibilityNodeInfo.ACTION_CLICK,
                            null
                    )) {
                        throw new OracleFailure("语义节点无法执行点击");
                    }
                    describeNode(node, result);
                });
                waitForIdle();
                return;
            case "set_text":
                String text = utf8Argument("value", true);
                withUniqueNode(node -> {
                    Bundle setText = new Bundle();
                    setText.putCharSequence(
                            AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                            text
                    );
                    if (!node.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
                            || !node.performAction(
                                    AccessibilityNodeInfo.ACTION_SET_TEXT,
                                    setText
                            )) {
                        throw new OracleFailure("语义节点无法设置 Unicode 文本");
                    }
                    describeNode(node, result);
                });
                waitForIdle();
                return;
            case "set_text_utf16_hex":
                String utf16Hex = requireUtf16HexArgument("value_utf16_hex");
                String rawUtf16 = stringFromUtf16Hex(utf16Hex);
                withUniqueNode(node -> {
                    Bundle setText = new Bundle();
                    setText.putCharSequence(
                            AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                            rawUtf16
                    );
                    if (!node.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
                            || !node.performAction(
                                    AccessibilityNodeInfo.ACTION_SET_TEXT,
                                    setText
                            )) {
                        throw new OracleFailure("语义节点无法设置 UTF-16 边界文本");
                    }
                    node.refresh();
                    String observed = asString(node.getText());
                    String observedHex = utf16Hex(observed);
                    result.putString("observed_utf16_hex", observedHex);
                    result.putBoolean("observed_utf16_well_formed", isWellFormedUtf16(observed));
                    if (!utf16Hex.equals(observedHex)) {
                        throw new OracleFailure(
                                "UTF-16 边界文本回读不一致，observed_utf16_hex="
                                        + observedHex
                        );
                    }
                });
                waitForIdle();
                return;
            case "scroll_forward":
                withUniqueNode(node -> {
                    if (!performOnSelfOrScrollableAncestor(
                            node,
                            AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
                    )) {
                        throw new OracleFailure("语义节点无法向前滚动");
                    }
                    describeNode(node, result);
                });
                waitForIdle();
                return;
            case "back":
                if (!automation.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)) {
                    throw new OracleFailure("无法执行系统返回操作");
                }
                waitForIdle();
                return;
            case "dump":
                writeTreeEvidence(result);
                return;
            case "screenshot":
                writeScreenshotEvidence(result);
                return;
            case "talkback_next":
                injectTalkBackNextGesture();
                waitForIdle();
                return;
            case "focus_trace":
                captureFocusTrace(result);
                return;
            case "transition_capture":
                captureTransition(result);
                return;
            case "idle_capture":
                captureIdle(result);
                return;
            case "normal_motion_capture":
                captureNormalMotion(result);
                return;
            case "character_boxes":
                captureCharacterBoxes(result);
                return;
            case "probe":
                AccessibilityNodeInfo root = waitForRoot(timeoutMs());
                result.putString("root_package", bounded(asString(root.getPackageName())));
                result.putString("root_class", bounded(asString(root.getClassName())));
                result.putInt("matching_nodes", resolveMatches(root).size());
                return;
            default:
                throw new OracleFailure("不支持的 action: " + action);
        }
    }

    private interface NodeAction {
        void run(AccessibilityNodeInfo node) throws Exception;
    }

    private void withUniqueNode(NodeAction action) throws Exception {
        action.run(requireUniqueNode());
    }

    private AccessibilityNodeInfo requireUniqueNode() {
        AccessibilityNodeInfo root = waitForRoot(timeoutMs());
        List<AccessibilityNodeInfo> matches = waitForMatches(root, timeoutMs());
        if (matches.size() != 1) {
            throw new OracleFailure("冻结选择器必须唯一，实际匹配 " + matches.size() + " 个节点");
        }
        return matches.get(0);
    }

    private List<AccessibilityNodeInfo> waitForMatches(
            AccessibilityNodeInfo initialRoot,
            long timeout
    ) {
        long deadline = SystemClock.uptimeMillis() + timeout;
        AccessibilityNodeInfo root = initialRoot;
        List<AccessibilityNodeInfo> matches = resolveMatches(root);
        while (matches.size() != 1 && SystemClock.uptimeMillis() < deadline) {
            SystemClock.sleep(100L);
            AccessibilityNodeInfo refreshed = automation.getRootInActiveWindow();
            if (refreshed != null) {
                root = refreshed;
            }
            matches = resolveMatches(root);
        }
        return matches;
    }

    private AccessibilityNodeInfo waitForRoot(long timeout) {
        long deadline = SystemClock.uptimeMillis() + timeout;
        AccessibilityNodeInfo root;
        do {
            root = automation.getRootInActiveWindow();
            if (root != null && TARGET_PACKAGE.equals(asString(root.getPackageName()))) {
                return root;
            }
            SystemClock.sleep(100L);
        } while (SystemClock.uptimeMillis() < deadline);
        throw new OracleFailure("在截止时间内未找到目标应用的活动无障碍窗口");
    }

    private List<AccessibilityNodeInfo> resolveMatches(AccessibilityNodeInfo root) {
        String selectorType = argument("selector_type", "any");
        String selector = utf8Argument("selector");
        String classSuffix = argument("class_suffix", "");
        if (!hasUtf8Argument("scope_selector")) {
            return findMatches(root, selectorType, selector, classSuffix);
        }
        String scopeType = argument("scope_selector_type", "any");
        String scopeSelector = utf8Argument("scope_selector");
        String scopeClassSuffix = argument("scope_class_suffix", "");
        List<AccessibilityNodeInfo> scopes = findMatches(
                root,
                scopeType,
                scopeSelector,
                scopeClassSuffix
        );
        if (scopes.size() != 1) {
            return scopes;
        }
        AccessibilityNodeInfo scope = scopes.get(0);
        List<AccessibilityNodeInfo> scopedMatches = findMatches(
                scope,
                selectorType,
                selector,
                classSuffix
        );
        if (!scopedMatches.isEmpty()) {
            return scopedMatches;
        }
        if (scopeSelector.startsWith("火种：")) {
            return new ArrayList<>();
        }
        if (!scopeSelector.matches("^删除“.+”吗？此操作无法撤销。$")) {
            return new ArrayList<>();
        }
        AccessibilityNodeInfo dialogAncestor = scope.getParent();
        for (int depth = 0;
                dialogAncestor != null && depth < MAX_TREE_DEPTH;
                depth++, dialogAncestor = dialogAncestor.getParent()) {
            scopedMatches = findMatches(
                    dialogAncestor,
                    selectorType,
                    selector,
                    classSuffix
            );
            if (!scopedMatches.isEmpty()) {
                return scopedMatches;
            }
        }
        return new ArrayList<>();
    }

    private List<AccessibilityNodeInfo> findMatches(
            AccessibilityNodeInfo root,
            String selectorType,
            String selector,
            String classSuffix
    ) {
        List<AccessibilityNodeInfo> matches = new ArrayList<>();
        ArrayList<AccessibilityNodeInfo> queue = new ArrayList<>();
        queue.add(root);
        for (int index = 0; index < queue.size(); index++) {
            if (queue.size() > MAX_TREE_NODES) {
                throw new OracleFailure("无障碍树超过节点上限");
            }
            AccessibilityNodeInfo node = queue.get(index);
            if (!TARGET_PACKAGE.equals(asString(node.getPackageName()))) {
                continue;
            }
            boolean textMatches = selector.equals(asString(node.getText()));
            boolean descriptionMatches = selector.equals(asString(node.getContentDescription()));
            boolean selected;
            switch (selectorType) {
                case "text":
                    selected = textMatches;
                    break;
                case "description":
                    selected = descriptionMatches;
                    break;
                case "any":
                    selected = textMatches || descriptionMatches;
                    break;
                default:
                    throw new OracleFailure("不支持的 selector_type: " + selectorType);
            }
            String className = asString(node.getClassName());
            if (selected && (classSuffix.isEmpty() || className.endsWith(classSuffix))) {
                matches.add(node);
            }
            for (int childIndex = 0; childIndex < node.getChildCount(); childIndex++) {
                AccessibilityNodeInfo child = node.getChild(childIndex);
                if (child != null) {
                    queue.add(child);
                }
            }
        }
        return matches;
    }

    private boolean performOnSelfOrClickableAncestor(
            AccessibilityNodeInfo node,
            int action,
            Bundle actionArguments
    ) {
        AccessibilityNodeInfo current = node;
        for (int depth = 0; current != null && depth < MAX_TREE_DEPTH; depth++) {
            if (current.isClickable()
                    && current.performAction(action, actionArguments)) {
                return true;
            }
            current = current.getParent();
        }
        return false;
    }

    private boolean performOnSelfOrScrollableAncestor(AccessibilityNodeInfo node, int action) {
        AccessibilityNodeInfo current = node;
        for (int depth = 0; current != null && depth < MAX_TREE_DEPTH; depth++) {
            if (current.isScrollable() && current.performAction(action)) {
                return true;
            }
            current = current.getParent();
        }
        return false;
    }

    private void writeTreeEvidence(Bundle result)
            throws JSONException, IOException, NoSuchAlgorithmException {
        AccessibilityNodeInfo root = waitForRoot(timeoutMs());
        JSONArray nodes = new JSONArray();
        appendNode(root, nodes, 0, "0");
        JSONObject document = new JSONObject();
        document.put("schema_version", 1);
        document.put("target_package", TARGET_PACKAGE);
        document.put("captured_elapsed_realtime_ms", SystemClock.elapsedRealtime());
        document.put("node_count", nodes.length());
        document.put("nodes", nodes);
        byte[] bytes = document.toString().getBytes(StandardCharsets.UTF_8);
        File file = privateEvidenceFile(requireEvidenceName(".json"));
        writeAtomic(file, bytes);
        reportFile(file, result);
    }

    private void appendNode(
            AccessibilityNodeInfo node,
            JSONArray output,
            int depth,
            String path
    ) throws JSONException {
        if (depth > MAX_TREE_DEPTH || output.length() >= MAX_TREE_NODES) {
            throw new OracleFailure("无障碍树超过冻结深度或节点上限");
        }
        if (!TARGET_PACKAGE.equals(asString(node.getPackageName()))) {
            return;
        }
        Rect bounds = new Rect();
        node.getBoundsInScreen(bounds);
        JSONObject item = new JSONObject();
        item.put("path", path);
        item.put("window_id", node.getWindowId());
        item.put("class", bounded(asString(node.getClassName())));
        putUtf8OrHex(item, "text", node.getText());
        putUtf8OrHex(item, "content_description", node.getContentDescription());
        item.put("view_id", bounded(node.getViewIdResourceName()));
        item.put("bounds", String.format(
                Locale.ROOT,
                "[%d,%d][%d,%d]",
                bounds.left,
                bounds.top,
                bounds.right,
                bounds.bottom
        ));
        item.put("clickable", node.isClickable());
        item.put("checkable", node.isCheckable());
        item.put("heading", node.isHeading());
        item.put("enabled", node.isEnabled());
        item.put("visible_to_user", node.isVisibleToUser());
        item.put("focusable", node.isFocusable());
        item.put("focused", node.isFocused());
        item.put("accessibility_focused", node.isAccessibilityFocused());
        item.put("scrollable", node.isScrollable());
        item.put("selected", node.isSelected());
        item.put("checked", node.isChecked());
        output.put(item);
        for (int index = 0; index < node.getChildCount(); index++) {
            AccessibilityNodeInfo child = node.getChild(index);
            if (child != null) {
                appendNode(child, output, depth + 1, path + "." + index);
            }
        }
    }

    private JSONObject captureHierarchySnapshot() throws JSONException {
        long start = SystemClock.uptimeMillis();
        AccessibilityNodeInfo root = automation.getRootInActiveWindow();
        if (root == null || !TARGET_PACKAGE.equals(asString(root.getPackageName()))) {
            throw new OracleFailure("事件绑定时没有目标应用 hierarchy root");
        }
        JSONArray nodes = new JSONArray();
        appendNode(root, nodes, 0, "0");
        JSONObject hierarchy = new JSONObject();
        hierarchy.put("capture_start_uptime_ms", start);
        hierarchy.put("root_window_id", root.getWindowId());
        hierarchy.put("nodes", nodes);
        hierarchy.put("capture_complete_uptime_ms", SystemClock.uptimeMillis());
        return hierarchy;
    }

    private String pixelBufferSha256(Bitmap bitmap)
            throws NoSuchAlgorithmException {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        int[] row = new int[bitmap.getWidth()];
        byte[] encoded = new byte[bitmap.getWidth() * 4];
        for (int y = 0; y < bitmap.getHeight(); y++) {
            bitmap.getPixels(row, 0, bitmap.getWidth(), 0, y, bitmap.getWidth(), 1);
            for (int x = 0; x < row.length; x++) {
                int pixel = row[x];
                int offset = x * 4;
                encoded[offset] = (byte) ((pixel >>> 24) & 0xff);
                encoded[offset + 1] = (byte) ((pixel >>> 16) & 0xff);
                encoded[offset + 2] = (byte) ((pixel >>> 8) & 0xff);
                encoded[offset + 3] = (byte) (pixel & 0xff);
            }
            digest.update(encoded);
        }
        StringBuilder value = new StringBuilder();
        for (byte item : digest.digest()) {
            value.append(String.format(Locale.ROOT, "%02x", item & 0xff));
        }
        return value.toString();
    }

    private static final class CapturedFrame {
        final int requestSequence;
        final long captureStart;
        final long captureComplete;
        final String name;
        final Rect contentBounds;
        final Bitmap content;

        CapturedFrame(
                int requestSequence,
                long captureStart,
                long captureComplete,
                String name,
                Rect contentBounds,
                Bitmap content
        ) {
            this.requestSequence = requestSequence;
            this.captureStart = captureStart;
            this.captureComplete = captureComplete;
            this.name = name;
            this.contentBounds = new Rect(contentBounds);
            this.content = content;
        }
    }

    private ThreadPoolExecutor frameEncoder() {
        return new ThreadPoolExecutor(
                2,
                2,
                0L,
                TimeUnit.MILLISECONDS,
                new ArrayBlockingQueue<>(2),
                new ThreadPoolExecutor.CallerRunsPolicy()
        );
    }

    private CapturedFrame captureApplicationContentFrame(String prefix, int index) {
        long captureStart = SystemClock.uptimeMillis();
        Bitmap screenshot = automation.takeScreenshot();
        if (screenshot == null) {
            throw new OracleFailure("frame 截图失败");
        }
        AccessibilityNodeInfo root = automation.getRootInActiveWindow();
        if (root == null || !TARGET_PACKAGE.equals(asString(root.getPackageName()))) {
            screenshot.recycle();
            throw new OracleFailure("frame 缺少目标应用 root");
        }
        Rect contentBounds = new Rect();
        root.getBoundsInScreen(contentBounds);
        contentBounds.intersect(
                0,
                0,
                screenshot.getWidth(),
                screenshot.getHeight()
        );
        if (contentBounds.isEmpty()) {
            screenshot.recycle();
            throw new OracleFailure("frame application bounds 为空");
        }
        Bitmap cropped = Bitmap.createBitmap(
                screenshot,
                contentBounds.left,
                contentBounds.top,
                contentBounds.width(),
                contentBounds.height()
        );
        Bitmap content = cropped.copy(Bitmap.Config.ARGB_8888, false);
        long captureComplete = SystemClock.uptimeMillis();
        if (content == null || content.isMutable()) {
            if (cropped != screenshot) {
                cropped.recycle();
            }
            screenshot.recycle();
            throw new OracleFailure("无法建立 immutable lossless frame buffer");
        }
        String name = String.format(Locale.ROOT, "%s-f%02d.png", prefix, index);
        if (cropped != screenshot) {
            cropped.recycle();
        }
        screenshot.recycle();
        return new CapturedFrame(
                index,
                captureStart,
                captureComplete,
                name,
                contentBounds,
                content
        );
    }

    private JSONObject publishCapturedFrame(CapturedFrame captured)
            throws IOException, JSONException, NoSuchAlgorithmException {
        File file = privateEvidenceFile(captured.name);
        File temporary = secureTemporary(file);
        String bufferDigest = pixelBufferSha256(captured.content);
        try {
            try (FileOutputStream stream = new FileOutputStream(temporary, false)) {
                if (!captured.content.compress(Bitmap.CompressFormat.PNG, 100, stream)) {
                    throw new OracleFailure("frame PNG 编码失败");
                }
                stream.getFD().sync();
            }
            publishTemporary(temporary, file);
        } finally {
            captured.content.recycle();
            if (temporary.exists() && !temporary.delete()) {
                throw new IOException("无法清理未发布的 frame 临时文件");
            }
        }
        JSONObject frame = new JSONObject();
        frame.put("request_sequence", captured.requestSequence);
        frame.put("capture_start_uptime_ms", captured.captureStart);
        frame.put("capture_complete_uptime_ms", captured.captureComplete);
        frame.put("evidence_name", captured.name);
        frame.put("bytes", file.length());
        frame.put("sha256", sha256(file));
        frame.put("pixel_buffer_format", "ARGB_8888_BIG_ENDIAN_ROW_MAJOR");
        frame.put("pixel_buffer_sha256", bufferDigest);
        frame.put("width", captured.contentBounds.width());
        frame.put("height", captured.contentBounds.height());
        frame.put(
                "application_content_bounds",
                String.format(
                        Locale.ROOT,
                        "[%d,%d][%d,%d]",
                        captured.contentBounds.left,
                        captured.contentBounds.top,
                        captured.contentBounds.right,
                        captured.contentBounds.bottom
                )
        );
        return frame;
    }

    private void captureTransition(Bundle result) throws Exception {
        String transitionId = requireArgument("transition_id");
        if (!transitionId.matches("R(?:0[2-9]|1[0-4])-[a-z0-9-]+")) {
            throw new OracleFailure("transition_id 不属于 R02-R14");
        }
        String transitionAction = requireArgument("transition_action");
        if (!transitionAction.equals("click") && !transitionAction.equals("back")) {
            throw new OracleFailure("transition_action 只允许 click/back");
        }
        String traceName = requireEvidenceName(".json");
        String prefix = traceName.substring(0, traceName.length() - 5);
        AtomicLong dispatch = new AtomicLong(-1L);
        AtomicInteger callbackSequence = new AtomicInteger(0);
        CopyOnWriteArrayList<JSONObject> events = new CopyOnWriteArrayList<>();
        automation.setOnAccessibilityEventListener(event -> {
            long dispatchValue = dispatch.get();
            int type = event.getEventType();
            if (dispatchValue < 0L
                    || event.getEventTime() < dispatchValue) {
                return;
            }
            JSONObject item = new JSONObject();
            try {
                item.put("callback_sequence", callbackSequence.incrementAndGet());
                item.put("event_time_ms", event.getEventTime());
                item.put("event_type", type);
                item.put("content_change_types", event.getContentChangeTypes());
                item.put("window_id", event.getWindowId());
                item.put("package", asString(event.getPackageName()));
                AccessibilityNodeInfo source = event.getSource();
                if (source == null) {
                    item.put("source", JSONObject.NULL);
                } else {
                    JSONObject serializedSource = new JSONObject();
                    serializedSource.put(
                            "package",
                            asString(source.getPackageName())
                    );
                    serializedSource.put("window_id", source.getWindowId());
                    serializedSource.put("class", asString(source.getClassName()));
                    String viewId = source.getViewIdResourceName();
                    serializedSource.put(
                            "view_id",
                            viewId == null ? JSONObject.NULL : viewId
                    );
                    putUtf8OrHex(serializedSource, "text", source.getText());
                    putUtf8OrHex(
                            serializedSource,
                            "content_description",
                            source.getContentDescription()
                    );
                    item.put("source", serializedSource);
                }
                if (TARGET_PACKAGE.equals(asString(event.getPackageName()))
                        && (type == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED
                        || type == AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED)) {
                    item.put("hierarchy", captureHierarchySnapshot());
                }
                events.add(item);
            } catch (Throwable error) {
                JSONObject failure = new JSONObject();
                try {
                    failure.put(
                            "callback_sequence",
                            callbackSequence.incrementAndGet()
                    );
                    failure.put("capture_error", safeUtf8String(error.getMessage()));
                    events.add(failure);
                } catch (JSONException impossible) {
                    throw new OracleFailure("无法序列化 event capture failure", impossible);
                }
            }
        });
        JSONObject document = new JSONObject();
        JSONArray frames = new JSONArray();
        List<Future<JSONObject>> encodedFrames = new ArrayList<>();
        ThreadPoolExecutor encoder = frameEncoder();
        long origin = SystemClock.uptimeMillis();
        try {
            document.put("schema_version", 1);
            document.put("clock", "android.os.SystemClock.uptimeMillis");
            document.put("transition_id", transitionId);
            document.put("transition_action", transitionAction);
            if (transitionAction.equals("click")) {
                document.put(
                        "selector_utf16_hex",
                        utf16Hex(utf8Argument("selector"))
                );
                if (hasUtf8Argument("scope_selector")) {
                    document.put(
                            "scope_selector_utf16_hex",
                            utf16Hex(utf8Argument("scope_selector"))
                    );
                }
            }
            document.put("before_hierarchy", captureHierarchySnapshot());
            for (int index = 0; index < 13; index++) {
                long target = origin + index * 100L;
                long remaining = target - SystemClock.uptimeMillis();
                if (remaining > 0L) {
                    SystemClock.sleep(remaining);
                }
                if (index == 2) {
                    AccessibilityNodeInfo transitionNode = null;
                    if (transitionAction.equals("click")) {
                        transitionNode = requireUniqueNode();
                    }
                    long actionDispatch;
                    long actionComplete;
                    if (transitionAction.equals("back")) {
                        actionDispatch = SystemClock.uptimeMillis();
                        dispatch.set(actionDispatch);
                        boolean succeeded = automation.performGlobalAction(
                                AccessibilityService.GLOBAL_ACTION_BACK);
                        actionComplete = SystemClock.uptimeMillis();
                        if (!succeeded) {
                            throw new OracleFailure("reduced-motion Android back 失败");
                        }
                    } else {
                        actionDispatch = SystemClock.uptimeMillis();
                        dispatch.set(actionDispatch);
                        boolean succeeded = performOnSelfOrClickableAncestor(
                                transitionNode,
                                AccessibilityNodeInfo.ACTION_CLICK,
                                null
                        );
                        actionComplete = SystemClock.uptimeMillis();
                        if (!succeeded) {
                            throw new OracleFailure(
                                    "reduced-motion scoped click 失败"
                            );
                        }
                    }
                    document.put("action_dispatch_uptime_ms", actionDispatch);
                    document.put("action_complete_uptime_ms", actionComplete);
                }
                CapturedFrame captured = captureApplicationContentFrame(prefix, index);
                encodedFrames.add(
                        encoder.submit(() -> publishCapturedFrame(captured))
                );
            }
        } finally {
            automation.setOnAccessibilityEventListener(null);
            encoder.shutdown();
        }
        for (Future<JSONObject> encoded : encodedFrames) {
            frames.put(encoded.get());
        }
        if (!encoder.awaitTermination(60L, TimeUnit.SECONDS)) {
            throw new OracleFailure("transition frame encoder 未在 60 秒内结束");
        }
        JSONArray serializedEvents = new JSONArray();
        List<JSONObject> sorted = new ArrayList<>(events);
        sorted.sort(
                Comparator.comparingInt(
                        value -> value.optInt("callback_sequence")
                )
        );
        for (JSONObject event : sorted) {
            serializedEvents.put(event);
        }
        document.put("events", serializedEvents);
        document.put("frames", frames);
        document.put("after_final_frame_hierarchy", captureHierarchySnapshot());
        File traceFile = privateEvidenceFile(traceName);
        writeAtomic(
                traceFile,
                document.toString().getBytes(StandardCharsets.UTF_8)
        );
        reportFile(traceFile, result);
        result.putInt("transition_event_count", serializedEvents.length());
        result.putInt("transition_frame_count", frames.length());
    }

    private void captureIdle(Bundle result) throws Exception {
        String stateId = requireArgument("state_id");
        if (!stateId.matches("R(?:01|04)-[a-z0-9-]+-idle")) {
            throw new OracleFailure("state_id 不属于冻结 idle 状态");
        }
        String traceName = requireEvidenceName(".json");
        String prefix = traceName.substring(0, traceName.length() - 5);
        JSONObject document = new JSONObject();
        document.put("schema_version", 1);
        document.put("clock", "android.os.SystemClock.uptimeMillis");
        document.put("state_id", stateId);
        document.put("before_hierarchy", captureHierarchySnapshot());
        JSONArray frames = new JSONArray();
        List<Future<JSONObject>> encodedFrames = new ArrayList<>();
        ThreadPoolExecutor encoder = frameEncoder();
        long origin = SystemClock.uptimeMillis();
        try {
            for (int index = 0; index < 26; index++) {
                long target = origin + index * 200L;
                long remaining = target - SystemClock.uptimeMillis();
                if (remaining > 0L) {
                    SystemClock.sleep(remaining);
                }
                CapturedFrame captured = captureApplicationContentFrame(prefix, index);
                encodedFrames.add(
                        encoder.submit(() -> publishCapturedFrame(captured))
                );
            }
        } finally {
            encoder.shutdown();
        }
        for (Future<JSONObject> encoded : encodedFrames) {
            frames.put(encoded.get());
        }
        if (!encoder.awaitTermination(60L, TimeUnit.SECONDS)) {
            throw new OracleFailure("idle frame encoder 未在 60 秒内结束");
        }
        document.put("frames", frames);
        document.put("after_hierarchy", captureHierarchySnapshot());
        File traceFile = privateEvidenceFile(traceName);
        writeAtomic(
                traceFile,
                document.toString().getBytes(StandardCharsets.UTF_8)
        );
        reportFile(traceFile, result);
        result.putInt("idle_frame_count", frames.length());
    }

    private void captureNormalMotion(Bundle result) throws Exception {
        String traceName = requireEvidenceName(".json");
        String prefix = traceName.substring(0, traceName.length() - 5);
        JSONObject document = new JSONObject();
        document.put("schema_version", 1);
        document.put("clock", "android.os.SystemClock.uptimeMillis");
        document.put("state_id", "normal-motion-onboarding");
        document.put("before_hierarchy", captureHierarchySnapshot());
        SystemClock.sleep(1_000L);
        JSONArray frames = new JSONArray();
        List<Future<JSONObject>> encodedFrames = new ArrayList<>();
        ThreadPoolExecutor encoder = frameEncoder();
        long origin = SystemClock.uptimeMillis();
        try {
            for (int index = 0; index < 60; index++) {
                long target = origin + index * 200L;
                long remaining = target - SystemClock.uptimeMillis();
                if (remaining > 0L) SystemClock.sleep(remaining);
                CapturedFrame captured = captureApplicationContentFrame(prefix, index);
                encodedFrames.add(
                        encoder.submit(() -> publishCapturedFrame(captured))
                );
            }
        } finally {
            encoder.shutdown();
        }
        for (Future<JSONObject> encoded : encodedFrames) {
            frames.put(encoded.get());
        }
        if (!encoder.awaitTermination(60L, TimeUnit.SECONDS)) {
            throw new OracleFailure("normal motion encoder 未在 60 秒内结束");
        }
        document.put("frames", frames);
        document.put("after_hierarchy", captureHierarchySnapshot());
        File traceFile = privateEvidenceFile(traceName);
        writeAtomic(
                traceFile,
                document.toString().getBytes(StandardCharsets.UTF_8)
        );
        reportFile(traceFile, result);
        result.putInt("normal_motion_frame_count", frames.length());
    }

    private void captureCharacterBoxes(Bundle result) throws Exception {
        String evidenceName = requireEvidenceName(".json");
        JSONObject document = new JSONObject();
        AccessibilityNodeInfo selected;
        String nodePath = arguments.getString("node_path");
        if (nodePath == null) {
            selected = requireUniqueNode();
        } else {
            selected = nodeAtPath(nodePath);
        }
        NodeAction capture = node -> {
            String text = asString(node.getText());
            if (text.isEmpty() || !isWellFormedUtf16(text)) {
                throw new OracleFailure("character boxes 要求非空合法 UTF-16 text");
            }
            Bundle arguments = new Bundle();
            arguments.putInt(
                    AccessibilityNodeInfo
                            .EXTRA_DATA_TEXT_CHARACTER_LOCATION_ARG_START_INDEX,
                    0
            );
            arguments.putInt(
                    AccessibilityNodeInfo
                            .EXTRA_DATA_TEXT_CHARACTER_LOCATION_ARG_LENGTH,
                    text.length()
            );
            if (!node.refreshWithExtraData(
                    AccessibilityNodeInfo.EXTRA_DATA_TEXT_CHARACTER_LOCATION_KEY,
                    arguments
            )) {
                throw new OracleFailure("节点拒绝 character location extra data");
            }
            Parcelable[] rawBoxes = node.getExtras().getParcelableArray(
                    AccessibilityNodeInfo.EXTRA_DATA_TEXT_CHARACTER_LOCATION_KEY
            );
            if (rawBoxes == null || rawBoxes.length != text.length()) {
                throw new OracleFailure("character location 数量不等于 UTF-16 length");
            }
            Rect nodeBounds = new Rect();
            node.getBoundsInScreen(nodeBounds);
            JSONArray boxes = new JSONArray();
            int codePointIndex = 0;
            for (int utf16Index = 0; utf16Index < text.length(); ) {
                int codePoint = text.codePointAt(utf16Index);
                int unitCount = Character.charCount(codePoint);
                RectF union = null;
                for (int offset = 0; offset < unitCount; offset++) {
                    Parcelable raw = rawBoxes[utf16Index + offset];
                    if (!(raw instanceof RectF)) {
                        throw new OracleFailure("character location 不是 RectF");
                    }
                    RectF box = new RectF((RectF) raw);
                    if (union == null) {
                        union = box;
                    } else {
                        union.union(box);
                    }
                }
                boolean whitespace = Character.isWhitespace(codePoint)
                        || Character.isSpaceChar(codePoint);
                if (!whitespace) {
                    if (union == null
                            || union.isEmpty()
                            || union.left < nodeBounds.left
                            || union.top < nodeBounds.top
                            || union.right > nodeBounds.right
                            || union.bottom > nodeBounds.bottom) {
                        throw new OracleFailure("非空白 code point box 越界或为空");
                    }
                    JSONObject item = new JSONObject();
                    item.put("code_point_index", codePointIndex);
                    item.put("utf16_start", utf16Index);
                    item.put("utf16_length", unitCount);
                    item.put(
                            "code_point",
                            String.format(Locale.ROOT, "U+%04X", codePoint)
                    );
                    item.put("left", union.left);
                    item.put("top", union.top);
                    item.put("right", union.right);
                    item.put("bottom", union.bottom);
                    boxes.put(item);
                }
                utf16Index += unitCount;
                codePointIndex++;
            }
            document.put("schema_version", 1);
            document.put("text", text);
            document.put("text_utf16_hex", utf16Hex(text));
            document.put("utf16_length", text.length());
            document.put("code_point_length", text.codePointCount(0, text.length()));
            document.put("non_whitespace_boxes", boxes);
            document.put(
                    "node_bounds",
                    String.format(
                            Locale.ROOT,
                            "[%d,%d][%d,%d]",
                            nodeBounds.left,
                            nodeBounds.top,
                            nodeBounds.right,
                            nodeBounds.bottom
                    )
            );
        };
        capture.run(selected);
        File file = privateEvidenceFile(evidenceName);
        writeAtomic(file, document.toString().getBytes(StandardCharsets.UTF_8));
        reportFile(file, result);
    }

    private AccessibilityNodeInfo nodeAtPath(String path) {
        if (!path.matches("0(?:\\.[0-9]+)*")) {
            throw new OracleFailure("node_path 不是规范 child-index path");
        }
        AccessibilityNodeInfo node = waitForRoot(timeoutMs());
        String[] components = path.split("\\.");
        for (int index = 1; index < components.length; index++) {
            int childIndex = Integer.parseInt(components[index]);
            if (childIndex < 0 || childIndex >= node.getChildCount()) {
                throw new OracleFailure("node_path 越出当前 hierarchy");
            }
            AccessibilityNodeInfo child = node.getChild(childIndex);
            if (child == null) {
                throw new OracleFailure("node_path 指向空 child");
            }
            node = child;
        }
        return node;
    }

    private void writeScreenshotEvidence(Bundle result)
            throws IOException, NoSuchAlgorithmException {
        Bitmap screenshot = automation.takeScreenshot();
        if (screenshot == null) {
            throw new OracleFailure("系统未返回无障碍截图");
        }
        File file = privateEvidenceFile(requireEvidenceName(".png"));
        File temporary = secureTemporary(file);
        try {
            try (FileOutputStream stream = new FileOutputStream(temporary, false)) {
                if (!screenshot.compress(Bitmap.CompressFormat.PNG, 100, stream)) {
                    throw new OracleFailure("PNG 编码失败");
                }
                stream.getFD().sync();
            }
            publishTemporary(temporary, file);
        } finally {
            screenshot.recycle();
            if (temporary.exists() && !temporary.delete()) {
                throw new IOException("无法清理未发布的截图临时文件");
            }
        }
        reportFile(file, result);
    }

    private void captureFocusTrace(Bundle result)
            throws JSONException, IOException, NoSuchAlgorithmException {
        int count = intArgument("count", 2, 100);
        long interval = longArgument("interval_ms", 700L, 100L, 10_000L);
        CopyOnWriteArrayList<JSONObject> events = new CopyOnWriteArrayList<>();
        JSONArray gestureStates = new JSONArray();
        automation.setOnAccessibilityEventListener(event -> {
            if (event.getEventType() != AccessibilityEvent.TYPE_VIEW_ACCESSIBILITY_FOCUSED) {
                return;
            }
            AccessibilityNodeInfo source = event.getSource();
            if (source == null) {
                return;
            }
            Rect bounds = new Rect();
            source.getBoundsInScreen(bounds);
            JSONObject item = new JSONObject();
            try {
                item.put("event_time_ms", event.getEventTime());
                item.put("package", bounded(asString(source.getPackageName())));
                item.put("class", bounded(asString(source.getClassName())));
                putUtf8OrHex(item, "text", source.getText());
                putUtf8OrHex(
                        item,
                        "content_description",
                        source.getContentDescription()
                );
                item.put("bounds", String.format(
                        Locale.ROOT,
                        "[%d,%d][%d,%d]",
                        bounds.left,
                        bounds.top,
                        bounds.right,
                        bounds.bottom
                ));
                item.put("heading", source.isHeading());
                item.put("checkable", source.isCheckable());
                item.put("clickable", source.isClickable());
                events.add(item);
            } catch (JSONException error) {
                throw new OracleFailure("无法序列化焦点事件", error);
            }
        });
        try {
            AccessibilityNodeInfo root = waitForRoot(timeoutMs());
            clearAccessibilityFocus(root);
            AccessibilityNodeInfo first = requireUniqueNode();
            Rect firstBounds = new Rect();
            first.getBoundsInScreen(firstBounds);
            if (firstBounds.isEmpty()) {
                throw new OracleFailure("首个 TalkBack 节点 bounds 为空");
            }
            injectExploreByTouch(firstBounds.centerX(), firstBounds.centerY());
            SystemClock.sleep(interval);
            gestureStates.put(accessibilityStateAfterGesture(0));
            for (int index = 1; index < count; index++) {
                injectTalkBackNextGesture();
                SystemClock.sleep(interval);
                gestureStates.put(accessibilityStateAfterGesture(index));
            }
        } finally {
            automation.setOnAccessibilityEventListener(null);
        }
        JSONObject document = new JSONObject();
        document.put("schema_version", 1);
        document.put("gesture_count", count);
        JSONArray serialized = new JSONArray();
        List<JSONObject> sorted = new ArrayList<>(events);
        sorted.sort(Comparator.comparingLong(value -> value.optLong("event_time_ms")));
        for (JSONObject event : sorted) {
            serialized.put(event);
        }
        document.put("focus_events", serialized);
        document.put("dumpsys_accessibility_after_each_gesture", gestureStates);
        File file = privateEvidenceFile(requireEvidenceName(".json"));
        writeAtomic(file, document.toString().getBytes(StandardCharsets.UTF_8));
        reportFile(file, result);
        result.putInt("focus_event_count", serialized.length());
    }

    private JSONObject accessibilityStateAfterGesture(int index)
            throws IOException, NoSuchAlgorithmException, JSONException {
        ParcelFileDescriptor descriptor = automation.executeShellCommand(
                "dumpsys accessibility"
        );
        byte[] raw;
        try (ParcelFileDescriptor owned = descriptor;
             FileInputStream input = new FileInputStream(owned.getFileDescriptor())) {
            raw = input.readNBytes(2_000_001);
        }
        if (raw.length > 2_000_000) {
            throw new OracleFailure("dumpsys accessibility 超过冻结 2MB 上限");
        }
        String text = new String(raw, StandardCharsets.UTF_8);
        JSONObject state = new JSONObject();
        state.put("gesture_index", index);
        state.put("bytes", raw.length);
        state.put(
                "sha256",
                lowercaseHex(MessageDigest.getInstance("SHA-256").digest(raw))
        );
        state.put("talkback_present", text.contains("TalkBack"));
        state.put(
                "touch_exploration_enabled",
                text.contains("touchExplorationEnabled=true")
        );
        if (!state.getBoolean("talkback_present")
                || !state.getBoolean("touch_exploration_enabled")) {
            throw new OracleFailure("gesture 后 TalkBack/touch exploration 未保持启用");
        }
        state.put("dumpsys_utf8", text);
        return state;
    }

    private void clearAccessibilityFocus(AccessibilityNodeInfo root) {
        ArrayList<AccessibilityNodeInfo> queue = new ArrayList<>();
        queue.add(root);
        for (int index = 0; index < queue.size(); index++) {
            AccessibilityNodeInfo node = queue.get(index);
            if (node.isAccessibilityFocused()) {
                node.performAction(AccessibilityNodeInfo.ACTION_CLEAR_ACCESSIBILITY_FOCUS);
            }
            for (int childIndex = 0; childIndex < node.getChildCount(); childIndex++) {
                AccessibilityNodeInfo child = node.getChild(childIndex);
                if (child != null) queue.add(child);
            }
        }
    }

    private void injectExploreByTouch(float x, float y) {
        long downTime = SystemClock.uptimeMillis();
        injectMotion(downTime, downTime, MotionEvent.ACTION_DOWN, x, y);
        injectMotion(downTime, downTime + 80L, MotionEvent.ACTION_UP, x, y);
    }

    private void injectTalkBackNextGesture() {
        DisplayManager manager = getContext().getSystemService(DisplayManager.class);
        Display display = manager == null
                ? null
                : manager.getDisplay(Display.DEFAULT_DISPLAY);
        if (display == null) {
            throw new OracleFailure("无法读取当前显示器");
        }
        android.graphics.Point size = new android.graphics.Point();
        display.getRealSize(size);
        float startX = size.x * 0.25f;
        float endX = size.x * 0.75f;
        float y = size.y * 0.50f;
        long downTime = SystemClock.uptimeMillis();
        injectMotion(downTime, downTime, MotionEvent.ACTION_DOWN, startX, y);
        int steps = 12;
        for (int index = 1; index < steps; index++) {
            long eventTime = downTime + index * 12L;
            float x = startX + (endX - startX) * index / steps;
            injectMotion(downTime, eventTime, MotionEvent.ACTION_MOVE, x, y);
        }
        injectMotion(
                downTime,
                downTime + steps * 12L,
                MotionEvent.ACTION_UP,
                endX,
                y
        );
    }

    private void injectMotion(
            long downTime,
            long eventTime,
            int action,
            float x,
            float y
    ) {
        MotionEvent event = MotionEvent.obtain(
                downTime,
                eventTime,
                action,
                x,
                y,
                0
        );
        event.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        try {
            if (!automation.injectInputEvent(event, true)) {
                throw new OracleFailure("系统拒绝 TalkBack 滑动输入");
            }
        } finally {
            event.recycle();
        }
    }

    private void waitForIdle() {
        try {
            automation.waitForIdle(100L, timeoutMs());
        } catch (java.util.concurrent.TimeoutException error) {
            throw new OracleFailure("应用在截止时间内没有进入空闲状态", error);
        }
    }

    private File privateEvidenceFile(String name) {
        File root = new File(getContext().getFilesDir(), "t01m-oracle");
        if (!root.exists() && !root.mkdirs()) {
            throw new OracleFailure("无法创建驱动私有证据目录");
        }
        try {
            String rootPath = root.getCanonicalPath();
            File file = new File(root, name);
            String filePath = file.getCanonicalPath();
            if (!filePath.startsWith(rootPath + File.separator)) {
                throw new OracleFailure("证据文件名越出私有目录");
            }
            return file;
        } catch (IOException error) {
            throw new OracleFailure("无法规范化证据路径", error);
        }
    }

    private String requireEvidenceName(String suffix) {
        String name = requireArgument("evidence_name");
        if (!name.matches("[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
                || !name.endsWith(suffix)
                || name.contains("..")) {
            throw new OracleFailure("证据文件名不符合冻结规则");
        }
        return name;
    }

    private static void writeAtomic(File file, byte[] bytes) throws IOException {
        File temporary = secureTemporary(file);
        try {
            try (FileOutputStream stream = new FileOutputStream(temporary, false)) {
                stream.write(bytes);
                stream.getFD().sync();
            }
            publishTemporary(temporary, file);
        } finally {
            if (temporary.exists() && !temporary.delete()) {
                throw new IOException("无法清理未发布的临时证据文件");
            }
        }
    }

    private static File secureTemporary(File destination) throws IOException {
        File parent = destination.getParentFile();
        if (parent == null || (!parent.isDirectory() && !parent.mkdirs())) {
            throw new IOException("证据目录不可用");
        }
        return File.createTempFile("." + destination.getName() + ".", ".tmp", parent);
    }

    private static void publishTemporary(File temporary, File destination)
            throws IOException {
        try {
            Files.move(
                    temporary.toPath(),
                    destination.toPath(),
                    StandardCopyOption.ATOMIC_MOVE,
                    StandardCopyOption.REPLACE_EXISTING
            );
        } finally {
            if (temporary.exists() && !temporary.delete()) {
                throw new IOException("无法清理未发布的临时证据文件");
            }
        }
    }

    private static void reportFile(File file, Bundle result)
            throws IOException, NoSuchAlgorithmException {
        result.putString("evidence_name", file.getName());
        result.putLong("evidence_bytes", file.length());
        result.putString("evidence_sha256", sha256(file));
    }

    private static String sha256(File file)
            throws IOException, NoSuchAlgorithmException {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        byte[] buffer = new byte[64 * 1024];
        try (FileInputStream input = new FileInputStream(file)) {
            int read;
            while ((read = input.read(buffer)) >= 0) {
                if (read > 0) {
                    digest.update(buffer, 0, read);
                }
            }
        }
        return lowercaseHex(digest.digest());
    }

    private static String lowercaseHex(byte[] raw) {
        StringBuilder value = new StringBuilder();
        for (byte item : raw) {
            value.append(String.format(Locale.ROOT, "%02x", item & 0xff));
        }
        return value.toString();
    }

    private static void describeNode(AccessibilityNodeInfo node, Bundle result) {
        Rect bounds = new Rect();
        node.getBoundsInScreen(bounds);
        putBundleUtf8OrHex(result, "observed_text", node.getText());
        putBundleUtf8OrHex(
                result,
                "observed_content_description",
                node.getContentDescription()
        );
        result.putString("observed_class", bounded(asString(node.getClassName())));
        result.putString(
                "observed_bounds",
                String.format(
                        Locale.ROOT,
                        "[%d,%d][%d,%d]",
                        bounds.left,
                        bounds.top,
                        bounds.right,
                        bounds.bottom
                )
        );
    }

    private String requireArgument(String name) {
        String value = arguments.getString(name);
        if (value == null || value.isEmpty()) {
            throw new OracleFailure("缺少参数: " + name);
        }
        if (value.length() > MAX_STRING_CHARS) {
            throw new OracleFailure("参数超过字符上限: " + name);
        }
        return value;
    }

    private String argument(String name, String fallback) {
        String value = arguments.getString(name);
        if (value == null) {
            return fallback;
        }
        if (value.length() > MAX_STRING_CHARS) {
            throw new OracleFailure("参数超过字符上限: " + name);
        }
        return value;
    }

    private String utf8Argument(String name) {
        return utf8Argument(name, false);
    }

    private String utf8Argument(String name, boolean allowEmpty) {
        String encoded = arguments.getString(name + "_base64");
        if (encoded == null) {
            return requireArgument(name);
        }
        if ((!allowEmpty && encoded.isEmpty())
                || encoded.length() > MAX_STRING_CHARS * 4) {
            throw new OracleFailure("Base64 参数长度无效: " + name);
        }
        final byte[] bytes;
        try {
            bytes = Base64.decode(encoded, Base64.NO_WRAP);
        } catch (IllegalArgumentException error) {
            throw new OracleFailure("Base64 参数无效: " + name, error);
        }
        try {
            String value = StandardCharsets.UTF_8.newDecoder()
                    .onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT)
                    .decode(java.nio.ByteBuffer.wrap(bytes))
                    .toString();
            if ((!allowEmpty && value.isEmpty()) || value.length() > MAX_STRING_CHARS) {
                throw new OracleFailure("UTF-8 参数长度无效: " + name);
            }
            return value;
        } catch (CharacterCodingException error) {
            throw new OracleFailure("Base64 参数不是合法 UTF-8: " + name, error);
        }
    }

    private String requireUtf16HexArgument(String name) {
        String value = requireArgument(name);
        if (!value.matches("(?:[0-9a-f]{4})+")
                || value.length() > MAX_STRING_CHARS * 4) {
            throw new OracleFailure(
                    "UTF-16 hex 必须是每 code unit 四位的小写 ASCII hex: " + name
            );
        }
        return value;
    }

    private static String stringFromUtf16Hex(String value) {
        char[] units = new char[value.length() / 4];
        for (int index = 0; index < units.length; index++) {
            units[index] = (char) Integer.parseInt(
                    value.substring(index * 4, index * 4 + 4),
                    16
            );
        }
        return new String(units);
    }

    private static String utf16Hex(CharSequence value) {
        if (value == null) {
            return "";
        }
        StringBuilder output = new StringBuilder(value.length() * 4);
        for (int index = 0; index < value.length(); index++) {
            output.append(String.format(Locale.ROOT, "%04x", (int) value.charAt(index)));
        }
        return output.toString();
    }

    private static boolean isWellFormedUtf16(CharSequence value) {
        if (value == null) {
            return true;
        }
        for (int index = 0; index < value.length(); index++) {
            char unit = value.charAt(index);
            if (Character.isHighSurrogate(unit)) {
                if (index + 1 >= value.length()
                        || !Character.isLowSurrogate(value.charAt(index + 1))) {
                    return false;
                }
                index++;
            } else if (Character.isLowSurrogate(unit)) {
                return false;
            }
        }
        return true;
    }

    private static void putUtf8OrHex(
            JSONObject output,
            String name,
            CharSequence value
    ) throws JSONException {
        String text = asString(value);
        boolean wellFormed = isWellFormedUtf16(text);
        output.put(name + "_utf8_valid", wellFormed);
        output.put(name + "_utf16_hex", utf16Hex(text));
        output.put(name, wellFormed ? bounded(text) : "");
    }

    private static void putBundleUtf8OrHex(
            Bundle output,
            String name,
            CharSequence value
    ) {
        String text = asString(value);
        boolean wellFormed = isWellFormedUtf16(text);
        output.putBoolean(name + "_utf8_valid", wellFormed);
        output.putString(name + "_utf16_hex", utf16Hex(text));
        output.putString(name, wellFormed ? bounded(text) : "");
    }

    private boolean hasUtf8Argument(String name) {
        return arguments.getString(name) != null
                || arguments.getString(name + "_base64") != null;
    }

    private int intArgument(String name, int minimum, int maximum) {
        String raw = requireArgument(name);
        try {
            int value = Integer.parseInt(raw);
            if (value < minimum || value > maximum) {
                throw new OracleFailure("参数超出范围: " + name);
            }
            return value;
        } catch (NumberFormatException error) {
            throw new OracleFailure("参数不是整数: " + name, error);
        }
    }

    private long longArgument(String name, long fallback, long minimum, long maximum) {
        String raw = arguments.getString(name);
        if (raw == null) {
            return fallback;
        }
        try {
            long value = Long.parseLong(raw);
            if (value < minimum || value > maximum) {
                throw new OracleFailure("参数超出范围: " + name);
            }
            return value;
        } catch (NumberFormatException error) {
            throw new OracleFailure("参数不是整数: " + name, error);
        }
    }

    private long timeoutMs() {
        return longArgument("timeout_ms", DEFAULT_TIMEOUT_MS, 100L, 60_000L);
    }

    private static String bounded(String value) {
        if (value == null) {
            return "";
        }
        if (value.length() <= MAX_STRING_CHARS) {
            return value;
        }
        int end = MAX_STRING_CHARS;
        if (end > 0 && Character.isHighSurrogate(value.charAt(end - 1))) {
            end--;
        }
        return value.substring(0, end);
    }

    private static String safeUtf8String(String value) {
        if (value == null) {
            return "";
        }
        if (isWellFormedUtf16(value)) {
            return bounded(value);
        }
        return "<invalid-utf16:" + utf16Hex(value) + ">";
    }

    private static String asString(CharSequence value) {
        return value == null ? "" : value.toString();
    }

    private static final class OracleFailure extends RuntimeException {
        OracleFailure(String message) {
            super(message);
        }

        OracleFailure(String message, Throwable cause) {
            super(message, cause);
        }
    }
}
