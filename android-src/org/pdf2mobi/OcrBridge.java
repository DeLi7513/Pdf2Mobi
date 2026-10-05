package org.pdf2mobi;

import android.graphics.Bitmap;

import com.google.mlkit.vision.common.InputImage;
import com.google.mlkit.vision.text.Text;
import com.google.mlkit.vision.text.TextRecognition;
import com.google.mlkit.vision.text.TextRecognizer;
import com.google.mlkit.vision.text.chinese.ChineseTextRecognizerOptions;
import com.google.mlkit.vision.text.latin.LatinTextRecognizerOptions;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Thin bridge between Python (via pyjnius) and ML Kit text recognition.
 *
 * ML Kit's API is asynchronous: results arrive on a listener thread. Python
 * needs a synchronous call, so this blocks on a latch with a timeout and
 * returns whatever text was recognised, or an empty string on failure. A
 * failure here must never crash the conversion, so every error path returns "".
 *
 * Chinese and Latin models are interchangeable; the Chinese recogniser also
 * handles Latin script and digits, so it is used whenever it is available.
 */
public final class OcrBridge {

    private static TextRecognizer recognizer;
    private static boolean chineseModel;

    private OcrBridge() {
    }

    private static synchronized TextRecognizer getRecognizer() {
        if (recognizer == null) {
            try {
                recognizer = TextRecognition.getClient(
                        new ChineseTextRecognizerOptions.Builder().build());
                chineseModel = true;
            } catch (Throwable t) {
                // Chinese model unavailable (not downloaded yet): fall back.
                recognizer = TextRecognition.getClient(
                        new LatinTextRecognizerOptions.Builder().build());
                chineseModel = false;
            }
        }
        return recognizer;
    }

    /** Recognise all text in the supplied bitmap. Never throws. */
    public static String recognize(Bitmap bitmap) {
        if (bitmap == null) {
            return "";
        }
        final CountDownLatch latch = new CountDownLatch(1);
        final AtomicReference<String> result = new AtomicReference<>("");
        try {
            InputImage image = InputImage.fromBitmap(bitmap, 0);
            getRecognizer().process(image)
                    .addOnSuccessListener(text -> {
                        result.set(buildText(text));
                        latch.countDown();
                    })
                    .addOnFailureListener(e -> {
                        result.set("");
                        latch.countDown();
                    });
            // 30 seconds is generous for one page but must not hang forever.
            latch.await(30, TimeUnit.SECONDS);
        } catch (Throwable t) {
            return "";
        }
        return result.get();
    }

    private static String buildText(Text text) {
        StringBuilder sb = new StringBuilder();
        for (Text.TextBlock block : text.getTextBlocks()) {
            for (Text.Line line : block.getLines()) {
                String s = line.getText();
                if (s != null && !s.trim().isEmpty()) {
                    sb.append(s.trim()).append('\n');
                }
            }
            // Blank line between blocks preserves paragraph structure.
            if (sb.length() > 0 && sb.charAt(sb.length() - 1) != '\n') {
                sb.append('\n');
            }
            sb.append('\n');
        }
        return sb.toString().trim();
    }

    /** Whether the Chinese model is in use, for display in the UI. */
    public static boolean isChineseModel() {
        return chineseModel;
    }
}
