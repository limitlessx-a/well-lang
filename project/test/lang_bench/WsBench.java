import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Base64;

public class WsBench {
    static final String GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

    static byte[] frame(int op, byte[] payload) {
        int n = payload.length;
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        out.write(0x80 | op);
        if (n < 126) {
            out.write(n);
        } else if (n < 65536) {
            out.write(126);
            out.write(n >> 8);
            out.write(n);
        } else {
            out.write(127);
            for (int i = 7; i >= 0; i--) out.write((int) ((long) n >> (8 * i)) & 0xff);
        }
        out.write(payload, 0, n);
        return out.toByteArray();
    }

    static void handle(Socket s) throws Exception {
        try (s) {
            InputStream in = new BufferedInputStream(s.getInputStream());
            OutputStream out = s.getOutputStream();
            ByteArrayOutputStream head = new ByteArrayOutputStream();
            int state = 0;
            while (state < 4) {
                int b = in.read();
                if (b < 0) return;
                head.write(b);
                if ((state == 0 || state == 2) && b == '\r') state++;
                else if ((state == 1 || state == 3) && b == '\n') state++;
                else state = 0;
            }
            String text = head.toString(StandardCharsets.ISO_8859_1);
            String key = null;
            for (String line : text.split("\r\n")) {
                int c = line.indexOf(':');
                if (c > 0 && line.substring(0, c).trim().equalsIgnoreCase("Sec-WebSocket-Key")) {
                    key = line.substring(c + 1).trim();
                }
            }
            if (key == null) return;
            MessageDigest sha1 = MessageDigest.getInstance("SHA-1");
            byte[] digest = sha1.digest((key + GUID).getBytes(StandardCharsets.ISO_8859_1));
            String accept = Base64.getEncoder().encodeToString(digest);
            String resp = "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n" +
                    "Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n";
            out.write(resp.getBytes(StandardCharsets.ISO_8859_1));
            out.write(frame(1, "ready".getBytes(StandardCharsets.UTF_8)));
            out.flush();
            while (true) {
                int b0 = in.read();
                int b1 = in.read();
                if (b0 < 0 || b1 < 0) return;
                int op = b0 & 0x0f;
                boolean masked = (b1 & 0x80) != 0;
                long n = b1 & 0x7f;
                if (n == 126) {
                    n = ((long) in.read() << 8) | in.read();
                } else if (n == 127) {
                    n = 0;
                    for (int i = 0; i < 8; i++) n = (n << 8) | in.read();
                }
                byte[] mask = new byte[4];
                if (masked) {
                    int r = in.readNBytes(mask, 0, 4);
                    if (r < 4) return;
                }
                byte[] payload = in.readNBytes((int) n);
                if (payload.length < n) return;
                if (masked) for (int i = 0; i < payload.length; i++) payload[i] ^= mask[i & 3];
                if (op == 8) return;
                if (op == 9) {
                    out.write(frame(10, payload));
                } else {
                    out.write(frame(1, payload));
                }
                out.flush();
            }
        }
    }

    public static void main(String[] args) throws Exception {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : 18101;
        try (ServerSocket ss = new ServerSocket(port)) {
            while (true) {
                Socket s = ss.accept();
                Thread t = new Thread(() -> {
                    try { handle(s); } catch (Exception ignored) { }
                });
                t.setDaemon(true);
                t.start();
            }
        }
    }
}
