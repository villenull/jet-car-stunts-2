import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.FileOutputStream;
import java.io.IOException;

/** One persistent UHID Xbox-compatible controller. No duplicate injected events. */
public final class Jcs2InputHelper {
  private static FileOutputStream uhid;
  private static int buttons, directions;
  private static final float[] axes = new float[6];
  // Linux Xbox keylayout expects 16-bit sticks and ABS_X,Y,RX,RY,Z,RZ.
  // Its stick flat=4096 must NOT be paired with the old +/-127 descriptor.
  private static final byte[] HID_DESC = new byte[] {
    0x05,0x01,0x09,0x05,(byte)0xa1,0x01,
    0x05,0x09,0x19,0x01,0x29,0x10,0x15,0x00,0x25,0x01,
    0x75,0x01,(byte)0x95,0x10,(byte)0x81,0x02,
    0x05,0x01,0x16,0x01,(byte)0x80,0x26,(byte)0xff,0x7f,
    0x75,0x10,(byte)0x95,0x06,
    0x09,0x30,0x09,0x31,0x09,0x33,0x09,0x34,0x09,0x32,0x09,0x35,(byte)0x81,0x02,
    0x15,0x00,0x25,0x07,0x75,0x08,(byte)0x95,0x01,0x09,0x39,(byte)0x81,0x42,
    (byte)0xc0
  };

  public static void main(String[] args) throws Exception {
    try {
      openUhid();
      int id = waitReady();
      sendHid();
      System.out.println("READY JCS2_UHID " + id); System.out.flush();
      BufferedReader in = new BufferedReader(new InputStreamReader(System.in));
      long deadline = System.nanoTime() + 6L * 60 * 60 * 1000000000L;
      String line;
      while ((line = in.readLine()) != null && System.nanoTime() < deadline) {
        String[] p = line.trim().split("\\s+");
        if ("key".equals(p[0]) && p.length == 3) {
          int code = Integer.parseInt(p[1]);
          if (!"down".equals(p[2]) && !"up".equals(p[2])) throw new IllegalArgumentException("key action");
          boolean down = "down".equals(p[2]);
          int bit = buttonBit(code);
          if (bit >= 0) buttons = down ? buttons | (1 << bit) : buttons & ~(1 << bit);
          else if (code >= 19 && code <= 22) {
            int mask = 1 << (code - 19);
            directions = down ? directions | mask : directions & ~mask;
          } else throw new IllegalArgumentException("unsupported key " + code);
        } else if ("axis".equals(p[0]) && p.length == 7) {
          for (int i = 0; i < 6; i++) {
            float v = Float.parseFloat(p[i + 1]);
            if (Float.isNaN(v) || Float.isInfinite(v)) throw new IllegalArgumentException("axis NaN");
            axes[i] = Math.max(i < 4 ? -1f : 0f, Math.min(1f, v));
          }
        } else if ("neutral".equals(p[0])) {
          buttons = directions = 0;
          java.util.Arrays.fill(axes, 0f);
        } else if ("quit".equals(p[0])) break;
        else throw new IllegalArgumentException("protocol");
        // Key transitions preserve all six current axes.
        sendHid();
        System.out.println("ACK"); System.out.flush();
      }
    } finally {
      if (uhid != null) {
        try { buttons = directions = 0; java.util.Arrays.fill(axes, 0f); sendHid(); } catch (IOException ignored) {}
        try { byte[] event = new byte[4]; put32(event, 0, 1); uhid.write(event); uhid.flush(); } catch (IOException ignored) {}
        uhid.close();
      }
    }
  }

  private static void openUhid() throws IOException {
    uhid = new FileOutputStream("/dev/uhid");
    byte[] e = new byte[280 + HID_DESC.length];
    put32(e, 0, 11); ascii(e, 4, 128, "JCS2 Virtual Xbox Controller");
    ascii(e, 132, 64, "jcs2/uhid"); ascii(e, 196, 64, "jcs2");
    put16(e, 260, HID_DESC.length); put16(e, 262, 3);
    put32(e, 264, 0x045e); put32(e, 268, 0x028e); put32(e, 272, 0x0100);
    System.arraycopy(HID_DESC, 0, e, 280, HID_DESC.length);
    uhid.write(e); uhid.flush();
  }

  private static int waitReady() throws Exception {
    Class<?> cls = Class.forName("android.hardware.input.InputManager");
    Object manager = cls.getMethod("getInstance").invoke(null);
    long end = System.nanoTime() + 4000000000L;
    while (System.nanoTime() < end) {
      int[] ids = (int[]) cls.getMethod("getInputDeviceIds").invoke(manager);
      for (int id : ids) {
        Object dev = cls.getMethod("getInputDevice", int.class).invoke(manager, id);
        if (dev == null) continue;
        Class<?> dc = dev.getClass();
        String name = (String) dc.getMethod("getName").invoke(dev);
        int sources = (Integer) dc.getMethod("getSources").invoke(dev);
        Object range = dc.getMethod("getMotionRange", int.class).invoke(dev, 0);
        if ("JCS2 Virtual Xbox Controller".equals(name) && (sources & 0x01000010) == 0x01000010 && range != null) return id;
      }
      Thread.sleep(50);
    }
    throw new IllegalStateException("JCS2 UHID InputDevice not enumerated");
  }

  private static byte[] hidReport() {
    byte[] e = new byte[21]; // UHID_INPUT2 header + 15-byte HID report
    put32(e, 0, 12); put16(e, 4, 15); put16(e, 6, buttons);
    for (int i = 0; i < 6; i++) {
      float v = i < 4 ? axes[i] : axes[i] * 2f - 1f;
      put16(e, 8 + i * 2, Math.round(v * 32767f));
    }
    int x = ((directions & 8) != 0 ? 1 : 0) - ((directions & 4) != 0 ? 1 : 0);
    int y = ((directions & 2) != 0 ? 1 : 0) - ((directions & 1) != 0 ? 1 : 0);
    e[20] = (byte)(y < 0 ? (x < 0 ? 7 : x > 0 ? 1 : 0) : y > 0 ? (x < 0 ? 5 : x > 0 ? 3 : 4) : x < 0 ? 6 : x > 0 ? 2 : 8);
    return e;
  }
  private static void sendHid() throws IOException { uhid.write(hidReport()); uhid.flush(); }
  private static int buttonBit(int code) {
    // HID buttons map consecutively from Linux BTN_GAMEPAD=304, including gaps.
    switch (code) {
      case 96: return 0; case 97: return 1; case 99: return 3; case 100: return 4;
      case 102: return 6; case 103: return 7; case 4: case 109: return 10;
      case 108: return 11; case 106: return 13; case 107: return 14;
      default: return -1;
    }
  }
  private static void put16(byte[] b, int o, int v) { b[o] = (byte)v; b[o+1] = (byte)(v >>> 8); }
  private static void put32(byte[] b, int o, int v) { for (int i=0; i<4; i++) b[o+i] = (byte)(v >>> (i*8)); }
  private static void ascii(byte[] b, int o, int n, String s) { byte[] x=s.getBytes(); System.arraycopy(x,0,b,o,Math.min(n,x.length)); }
}
