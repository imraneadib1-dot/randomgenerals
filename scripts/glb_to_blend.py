"""A relo 1.0 GLB -> a ready-to-open .blend. Runs inside Blender:

    blender -b --factory-startup --python scripts/glb_to_blend.py -- in.glb out.blend

Used by model3d.make_blend() for "Download for Blender". Written for
Blender 4.0 (Ubuntu's package on the server) through 5.x.

What the file has that a plain GLB import does not:
  - the model as one object named relo_model, standing on the floor
    (its lowest point at z=0), centred on the origin
  - every texture PACKED into the .blend, so it is one file to move
  - the texture's alpha wired into the material when the texture has
    transparent pixels (the GLB is exported opaque, which left that as a
    manual step)
  - a camera framing the model and three area lights, so F12 renders it
"""
import math
import sys

import array

import bpy
from mathutils import Vector

try:                      # bundled with blender.org builds, not with Ubuntu's
    import numpy as np
except ImportError:
    np = None


def main():
    args = sys.argv[sys.argv.index("--") + 1:]
    src, dst = args[0], args[1]

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=src)
    scene = bpy.context.scene

    meshes = [o for o in scene.objects if o.type == "MESH"]
    if not meshes:
        raise SystemExit("no mesh in the GLB")

    # One object, named, with nothing above it: the importer adds an
    # empty parent for the glTF node tree, which only gets in the way.
    for o in meshes:
        if o.parent:
            mw = o.matrix_world.copy()
            o.parent = None
            o.matrix_world = mw
    for o in list(scene.objects):
        if o.type == "EMPTY" and not o.children:
            bpy.data.objects.remove(o, do_unlink=True)
    bpy.ops.object.select_all(action="DESELECT")
    for o in meshes:
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1:
        bpy.ops.object.join()
    model = bpy.context.view_layer.objects.active
    model.name = "relo_model"
    model.data.name = "relo_model"
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

    # On the floor, centred.
    corners = [model.matrix_world @ Vector(c) for c in model.bound_box]
    lo = Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners)))
    hi = Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners)))
    centre = (lo + hi) / 2
    model.location -= Vector((centre.x, centre.y, lo.z))
    bpy.ops.object.transform_apply(location=True, rotation=False, scale=False)
    size = max((hi - lo).length, 0.001)
    height = hi.z - lo.z

    # Materials: name them, and wire alpha where the texture has any.
    for i, slot in enumerate(model.material_slots):
        mat = slot.material
        if not mat or not mat.use_nodes:
            continue
        mat.name = "relo_material" if i == 0 else "relo_material_%d" % i
        nodes, links = mat.node_tree.nodes, mat.node_tree.links
        bsdf = next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)
        if not bsdf or bsdf.inputs["Alpha"].is_linked:
            continue
        base = bsdf.inputs["Base Color"]
        tex = base.links[0].from_node if base.is_linked else None
        if not tex or tex.type != "TEX_IMAGE" or not tex.image:
            continue
        img = tex.image
        if img.channels < 4 or not img.size[0]:
            continue
        w, h = img.size
        if w * h > 4096 * 4096:
            continue
        if np is not None:
            rgba = np.empty(w * h * 4, dtype=np.float32)
            img.pixels.foreach_get(rgba)
            lowest = float(rgba[3::4].min())
        else:
            rgba = array.array("f", bytes(4 * w * h * 4))
            img.pixels.foreach_get(rgba)
            lowest = min(rgba[3::4])
        if lowest < 0.98:
            links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
            if hasattr(mat, "blend_method"):
                mat.blend_method = "HASHED"

    # Everything in one file.
    for img in bpy.data.images:
        if img.source == "FILE" and not img.packed_file:
            try:
                img.pack()
            except RuntimeError:
                pass

    # A camera that frames it, three-quarter view, and soft lights.
    cam_data = bpy.data.cameras.new("relo_camera")
    cam = bpy.data.objects.new("relo_camera", cam_data)
    scene.collection.objects.link(cam)
    dist = size * 1.6
    cam.location = (dist * 0.75, -dist * 0.95, height * 0.55 + dist * 0.45)
    target = Vector((0, 0, height * 0.45))
    cam.rotation_euler = (target - cam.location).to_track_quat("-Z", "Y").to_euler()
    scene.camera = cam
    for name, loc, energy in (("relo_key", (1.2, -1.0, 1.4), 600),
                              ("relo_fill", (-1.4, -0.6, 0.8), 250),
                              ("relo_rim", (0.0, 1.5, 1.2), 300)):
        light = bpy.data.objects.new(name, bpy.data.lights.new(name, "AREA"))
        light.data.energy = energy * size * size
        light.data.size = size
        light.location = Vector(loc) * size * 1.4
        light.rotation_euler = (target - light.location).to_track_quat("-Z", "Y").to_euler()
        scene.collection.objects.link(light)
    world = bpy.data.worlds.new("relo_world")
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.05, 0.05, 0.055, 1)
        bg.inputs[1].default_value = 1.0
    scene.world = world
    scene.render.resolution_x = 1600
    scene.render.resolution_y = 1200
    scene.unit_settings.system = "METRIC"

    bpy.ops.object.select_all(action="DESELECT")
    model.select_set(True)
    bpy.context.view_layer.objects.active = model
    bpy.ops.wm.save_as_mainfile(filepath=dst, compress=True, check_existing=False)
    print("RELO_BLEND_OK", dst, "faces", len(model.data.polygons), "size", round(size, 3))


main()
