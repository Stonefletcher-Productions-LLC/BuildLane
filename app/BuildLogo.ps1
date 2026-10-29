# Draws the BuildLane logo and writes every place it is used, so the brand is reproducible instead of a binary nobody
# can edit: app\BuildLane.ico (window, taskbar, desktop shortcut, launcher exe) and docs\images\logo*.png (the README
# header and GitHub's social preview).
# Usage: powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\BuildLogo.ps1 [-PreviewDirectory <dir>]
#
# The mark: a "B" for BuildLane on a deep navy tile, over a lane stripe that runs from cyan to green. It shares no
# shape, silhouette or colour with the LifeShare icon (a coral house with a teal gear) on purpose: two products, two
# icons, even though one built the other.
param([string]$PreviewDirectory = "")

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Drawing.Text;
using System.IO;
using System.Runtime.InteropServices;

public static class BuildLaneLogo
{
    static readonly Color Ink0 = Color.FromArgb(0x27, 0x39, 0x51);   // tile top
    static readonly Color Ink1 = Color.FromArgb(0x0F, 0x17, 0x21);   // tile bottom
    static readonly Color Cyan = Color.FromArgb(0x4C, 0xC9, 0xF5);
    static readonly Color Green = Color.FromArgb(0x3D, 0xDC, 0x97);

    static GraphicsPath RoundRect(RectangleF r, float radius)
    {
        var p = new GraphicsPath();
        float d = radius * 2f;
        if (d <= 0) { p.AddRectangle(r); return p; }
        p.AddArc(r.X, r.Y, d, d, 180, 90);
        p.AddArc(r.Right - d, r.Y, d, d, 270, 90);
        p.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
        p.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
        p.CloseFigure();
        return p;
    }

    // Every coordinate is expressed for a 256 px canvas and multiplied by k, so one drawing scales to all sizes.
    public static Bitmap Render(int size)
    {
        float k = size / 256f;
        var bmp = new Bitmap(size, size, PixelFormat.Format32bppArgb);
        using (var g = Graphics.FromImage(bmp))
        {
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.PixelOffsetMode = PixelOffsetMode.HighQuality;
            g.InterpolationMode = InterpolationMode.HighQualityBicubic;
            g.TextRenderingHint = TextRenderingHint.AntiAliasGridFit;
            g.Clear(Color.Transparent);

            using (var tile = RoundRect(new RectangleF(3 * k, 3 * k, 250 * k, 250 * k), 58 * k))
            {
                using (var fill = new LinearGradientBrush(new PointF(0, 0), new PointF(0, size), Ink0, Ink1))
                    g.FillPath(fill, tile);
                // A hairline highlight reads as a lit edge at 48 px and above; below that it only muddies the tile.
                if (size >= 48)
                    using (var edge = new Pen(Color.FromArgb(46, 255, 255, 255), Math.Max(1f, 2.2f * k)))
                        g.DrawPath(edge, tile);
            }

            // The lane: the stripe the work runs along, and the reason the product is not just "Build".
            using (var lane = RoundRect(new RectangleF(58 * k, 170 * k, 140 * k, 30 * k), 15 * k))
            using (var fill = new LinearGradientBrush(new PointF(58 * k, 0), new PointF(198 * k, 0), Cyan, Green))
                g.FillPath(fill, lane);

            // Two lane dashes across the stripe. Below 32 px they would be a single smudge, so small sizes keep the
            // stripe solid and let the colour carry the shape.
            if (size >= 32)
            {
                using (var dash = new SolidBrush(Color.FromArgb(220, 0x0F, 0x17, 0x21)))
                {
                    g.FillRectangle(dash, 96 * k, 172 * k, 13 * k, 26 * k);
                    g.FillRectangle(dash, 132 * k, 172 * k, 13 * k, 26 * k);
                }
            }

            // The letter, measured so it stays optically centred whatever the renderer does with the font metrics.
            using (var font = new Font("Segoe UI", 176 * k, FontStyle.Bold, GraphicsUnit.Pixel))
            using (var path = new GraphicsPath())
            {
                path.AddString("B", font.FontFamily, (int)FontStyle.Bold, 176 * k, new PointF(0, 0), StringFormat.GenericTypographic);
                var bounds = path.GetBounds();
                var target = new RectangleF(24 * k, 22 * k, 208 * k, 118 * k);
                var scale = Math.Min(target.Width / bounds.Width, target.Height / bounds.Height);
                using (var placed = new Matrix())
                {
                    // Scale about the origin first, then move the scaled glyph into place: in GDI+ Translate prepends,
                    // so doing it the other way round leaves the glyph at its original size.
                    placed.Scale(scale, scale);
                    placed.Translate(target.X + (target.Width - bounds.Width * scale) / 2f - bounds.X * scale,
                                     target.Y + (target.Height - bounds.Height * scale) / 2f - bounds.Y * scale);
                    path.Transform(placed);
                }
                g.FillPath(Brushes.White, path);
            }
        }
        return bmp;
    }

    // 32-bit DIB frames below 256 px (the form's Icon loads those directly) and a PNG frame at 256, which is what
    // Windows wants for the large tile and what the previous icon already did.
    public static void SaveIco(string path, int[] sizes)
    {
        var images = new List<byte[]>();
        var metas = new List<int[]>();
        foreach (int s in sizes)
        {
            using (var bmp = Render(s))
            {
                byte[] data;
                if (s >= 128)
                {
                    using (var ms = new MemoryStream()) { bmp.Save(ms, ImageFormat.Png); data = ms.ToArray(); }
                }
                else data = Dib(bmp);
                images.Add(data);
                metas.Add(new[] { s, data.Length });
            }
        }

        using (var fs = new FileStream(path, FileMode.Create, FileAccess.Write))
        using (var w = new BinaryWriter(fs))
        {
            w.Write((short)0); w.Write((short)1); w.Write((short)images.Count);
            int offset = 6 + 16 * images.Count;
            for (int i = 0; i < images.Count; i++)
            {
                int s = metas[i][0];
                w.Write((byte)(s >= 256 ? 0 : s)); w.Write((byte)(s >= 256 ? 0 : s));
                w.Write((byte)0); w.Write((byte)0);
                w.Write((short)1); w.Write((short)32);
                w.Write(metas[i][1]); w.Write(offset);
                offset += metas[i][1];
            }
            foreach (var data in images) w.Write(data);
        }
    }

    static byte[] Dib(Bitmap bmp)
    {
        int s = bmp.Width;
        int maskStride = ((s + 31) / 32) * 4;
        using (var ms = new MemoryStream())
        using (var w = new BinaryWriter(ms))
        {
            w.Write(40); w.Write(s); w.Write(s * 2); w.Write((short)1); w.Write((short)32);
            w.Write(0); w.Write(s * s * 4); w.Write(0); w.Write(0); w.Write(0); w.Write(0);
            var rect = new Rectangle(0, 0, s, s);
            BitmapData d = bmp.LockBits(rect, ImageLockMode.ReadOnly, PixelFormat.Format32bppArgb);
            try
            {
                var row = new byte[s * 4];
                for (int y = s - 1; y >= 0; y--)   // DIB rows are bottom-up
                {
                    Marshal.Copy(new IntPtr(d.Scan0.ToInt64() + y * d.Stride), row, 0, row.Length);
                    w.Write(row);
                }
            }
            finally { bmp.UnlockBits(d); }
            for (int y = 0; y < s; y++) for (int x = 0; x < maskStride; x++) w.Write((byte)0);
            return ms.ToArray();
        }
    }

    // 1280x640 social preview: the tile, the wordmark and the one-line promise.
    public static void SaveSocial(string path)
    {
        using (var bmp = new Bitmap(1280, 640, PixelFormat.Format32bppArgb))
        using (var g = Graphics.FromImage(bmp))
        {
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.TextRenderingHint = TextRenderingHint.AntiAliasGridFit;
            using (var back = new LinearGradientBrush(new PointF(0, 0), new PointF(1280, 640), Color.FromArgb(0x10, 0x18, 0x22), Color.FromArgb(0x1B, 0x2A, 0x3B)))
                g.FillRectangle(back, 0, 0, 1280, 640);
            using (var tile = Render(268))
                g.DrawImage(tile, 96, 186, 268, 268);
            using (var title = new Font("Segoe UI", 96, FontStyle.Bold, GraphicsUnit.Pixel))
                g.DrawString("BuildLane", title, Brushes.White, 416, 214);
            using (var line = new Font("Segoe UI", 34, FontStyle.Regular, GraphicsUnit.Pixel))
                g.DrawString("Watch the build, land the changes, ship the release.", line, new SolidBrush(Color.FromArgb(0xC6, 0xD2, 0xE0)), 421, 330);
            using (var lane = RoundRect(new RectangleF(421, 402, 210, 12), 6))
            using (var fill = new LinearGradientBrush(new PointF(421, 0), new PointF(631, 0), Cyan, Green))
                g.FillPath(fill, lane);
            using (var small = new Font("Segoe UI", 24, FontStyle.Regular, GraphicsUnit.Pixel))
                g.DrawString("Windows desktop dashboard for a self-hosted build lane", small, new SolidBrush(Color.FromArgb(0x8F, 0x9F, 0xB3)), 421, 452);
            bmp.Save(path, ImageFormat.Png);
        }
    }
}
'@

$AppRoot = $PSScriptRoot
$RepoRoot = Split-Path $AppRoot -Parent
$IcoPath = Join-Path $AppRoot 'BuildLane.ico'
$Images = Join-Path $RepoRoot 'docs\images'

[BuildLaneLogo]::SaveIco($IcoPath, @(16, 24, 32, 48, 64, 128, 256))
Write-Output "wrote $IcoPath"

$Logo = Join-Path $Images 'logo.png'
$bmp = [BuildLaneLogo]::Render(512)
try { $bmp.Save($Logo, [Drawing.Imaging.ImageFormat]::Png) } finally { $bmp.Dispose() }
Write-Output "wrote $Logo"

$Social = Join-Path $Images 'social-preview.png'
[BuildLaneLogo]::SaveSocial($Social)
Write-Output "wrote $Social"

# A strip of the real sizes, so small-icon legibility can be checked instead of assumed.
if ($PreviewDirectory) {
    [void][IO.Directory]::CreateDirectory($PreviewDirectory)
    $sizes = @(16, 24, 32, 48, 64, 128, 256)
    $width = ($sizes | Measure-Object -Sum).Sum + 20 * ($sizes.Count + 1)
    $strip = New-Object Drawing.Bitmap -ArgumentList ([int]($width + 320)), 300
    $g = [Drawing.Graphics]::FromImage($strip)
    try {
        $g.Clear([Drawing.Color]::FromArgb(245, 247, 250))
        $x = 20
        foreach ($s in $sizes) {
            $b = [BuildLaneLogo]::Render($s)
            try { $g.DrawImage($b, $x, 20, $s, $s) } finally { $b.Dispose() }
            $f = New-Object Drawing.Font('Segoe UI', 9)
            $g.DrawString([string]$s, $f, [Drawing.Brushes]::Black, $x, 20 + $s + 4)
            $f.Dispose()
            $x += $s + 20
        }
    } finally { $g.Dispose() }
    $stripPath = Join-Path $PreviewDirectory 'logo-sizes.png'
    $strip.Save($stripPath, [Drawing.Imaging.ImageFormat]::Png)
    $strip.Dispose()
    Write-Output "wrote $stripPath"
}


