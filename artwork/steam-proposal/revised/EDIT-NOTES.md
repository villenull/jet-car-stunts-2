# Library artwork revisions

Tool: built-in image_gen.

Cover: library-cover.png, 1024×1536 (2:3). Generated edit of the previously downloaded cover. Original remains in ../originals/igdb-cover.jpg.

Cover prompt: Preserve the yellow/turquoise jet car, diagonal pose, framing and illustrated style. Restore crisp contours and defined tires/jet details. Extend gray directional speed streaks seamlessly into the top/bottom padding without zooming or cropping away car detail. Center both title lines individually on the horizontal canvas axis: JET CAR above STUNTS 2. Preserve red JET/STUNTS, white CAR, yellow 2. Keep title near the bottom with side margins, no added words.

Logo prompt: Preserve original angular lettering and colors. Center JET CAR directly above STUNTS 2 with a shared horizontal midpoint, then center the whole group horizontally and vertically. Output genuine transparent RGBA PNG, no baked checkerboard or opaque background. A retry additionally requested removal of asymmetric speed trails. Both generated logo attempts returned RGB checkerboard images; neither is a usable transparent deliverable.

Final logo: user-authorized direct pixel edit of the original RGBA logo using Pillow. Both rows are aligned by opaque letterform centers, rather than faded speed trails. Lettering group is centered at (640,360) on a 1280×720 canvas; original lettering, trails, shadows and alpha are retained. Alpha range verified 0–255.

Cover generated master: 1024×1536. library-cover-600x900.png is its proportional Steam-sized export.
