import os
from xml.etree import ElementTree as ElTree
import yaml
from PIL import Image
import re
from warnings import warn

from knowledge_representation.map_image_utils import point_to_map_coords

svg_el = "{http://www.w3.org/2000/svg}svg"
image_el = "{http://www.w3.org/2000/svg}image"
text_el = "{http://www.w3.org/2000/svg}text"
circle_el = "{http://www.w3.org/2000/svg}circle"
line_el = "{http://www.w3.org/2000/svg}line"
poly_el = "{http://www.w3.org/2000/svg}polygon"
path_el = "{http://www.w3.org/2000/svg}path"
group_el = "{http://www.w3.org/2000/svg}g"
tspan_el = "{http://www.w3.org/2000/svg}tspan"

float_pattern = r"[-+]?\d*\.\d+|[-+]?\d+"
translation_pattern = r"^translate\(({})\,({})\)".format(float_pattern, float_pattern)


def float_s3(string):
    return round(float(string), 3)


def populate_doors(doors, map):
    door_count = 0
    point_count = 0
    for name, ((x_0, y_0), (x_1, y_1)), approach_points in doors:
        door = map.add_door(name, x_0, y_0, x_1, y_1)
        if not door.is_valid():
            warn("Failed to add door '{}': ({},{}) ({},{})".format(name, x_0, y_0, x_0, y_1))
            continue
        for i, (x, y) in enumerate(approach_points):
            point = map.add_point("{}_approach{}".format(name, i), x, y)
            if not point.is_valid():
                warn("Failed to add approach {} for door '{}': {}, {}".format(i, name, x, y))
                continue
            point.add_attribute("approach_to", door)
            point_count += 1
        door_count += 1
    return door_count, point_count


def populate_with_map_annotations(ltmc, map_name, points, poses, regions, doors):
    """
    Inserts a map and supporting geometry into a knowledgebase. Any existing map by the name will be deleted

    Emits warnings for any annotation that can't be added, as when there are name collisions.
    :param ltmc: The knowledgebase to insert into
    :param map_name: Name of the map to create.
    :param points:
    :param poses:
    :param regions:
    :param doors:
    :return: a tuple of counts of how many of each type of annotation were successfully inserted
    """
    # Wipe any existing map by this name
    map = ltmc.get_map(map_name)
    map.delete()
    map = ltmc.get_map(map_name)
    point_count = 0
    pose_count = 0
    region_count = 0
    door_count = 0

    for name, point in points:
        point = map.add_point(name, *point)
        if not point.is_valid():
            warn("Failed to add point '{}': {}".format(name, *point))
        else:
            point_count += 1

    for name, (p1_x, p1_y), (p2_x, p2_y) in poses:
        pose = map.add_pose(name, p1_x, p1_y, p2_x, p2_y)
        if not pose.is_valid():
            warn("Failed to add pose '{}': {}".format(name, *((p1_x, p1_y), (p2_x, p2_y))))
        else:
            pose_count += 1

    for name, points in regions:
        region = map.add_region(name, points)
        if not region.is_valid():
            warn("Failed to add region '{}': {}".format(name, *points))
        else:
            region_count += 1

    door_count, extra_points = populate_doors(doors, map)
    point_count += extra_points
    return point_count, pose_count, region_count, door_count


def load_map_from_yaml(path_to_yaml, use_pixel_coords=False):
    """
    Attempt to load map annotations given a path to a YAML file. Emits warnings for issues with particular annotations.

    The PGM and the SVG will be loaded in the process. The SVG file to load is determined by the `annotations` key,
    or is an SVG with the same name as the YAML file.
    :param path_to_yaml:
    :return: a tuple of the name of the map and a nested tuple of points, poses and regions
    """
    parent_dir = os.path.dirname(path_to_yaml)
    yaml_name = os.path.basename(path_to_yaml).split(".")[0]
    with open(path_to_yaml) as map_yaml:
        map_metadata = yaml.load(map_yaml, Loader=yaml.FullLoader)
    map_metadata["name"] = yaml_name
    image_path = os.path.join(parent_dir, map_metadata["image"])
    map_image = Image.open(image_path)
    map_metadata["width"] = map_image.size[0]
    map_metadata["height"] = map_image.size[1]

    if "annotations" not in map_metadata:
        # Fallback when no annotations key is given: look for an svg
        # file that has the same name as the yaml file
        annotation_path = os.path.join(parent_dir, yaml_name + ".svg")
    else:
        annotation_path = os.path.join(parent_dir, map_metadata["annotations"])

    if not os.path.isfile(annotation_path):
        # No annotations to load. Since you're trying to load annotations, this is probably an error of some sort
        warn("No annotation file found at {}".format(annotation_path))
        return yaml_name, None

    with open(annotation_path) as test_svg:
        svg_data = test_svg.readlines()
        svg_data = " ".join(svg_data)

    svg_problems = check_svg_valid(svg_data, map_metadata)
    for prob in svg_problems:
        warn(prob)
    annotations = load_svg(svg_data)
    if not use_pixel_coords:
        annotations = transform_to_map_coords(map_metadata, *annotations)
    return map_metadata, annotations


def check_svg_valid(svg_data, map_info):
    problems = []
    tree = ElTree.fromstring(svg_data)
    # Root of tree is the SVG element
    svg = tree
    image = tree.find(image_el)

    def ori_is_zero(element):
        # Annotation tool may not add this
        if 'x' not in element.attrib:
            return
        ori_x = float(element.attrib['x'])
        ori_y = float(element.attrib['y'])

        if ori_x != 0 or ori_y != 0:
            problems.append("Image origin is ({}, {}) not (0, 0)".format(ori_x, ori_y))

    def dim_match(element, w, h):
        # Annotation tool may not add this
        if 'width' not in element.attrib:
            return
        e_w = float(element.attrib['width'])
        e_h = float(element.attrib['height'])

        if e_w != w or e_h != h:
            problems.append(
                "SVG or image dimensions are {}x{}, but YAML says they should be {}x{}".format(e_w, e_h, w, h))

    viewbox = svg.attrib["viewBox"]
    target_viewbox = "0 0 {} {}".format(map_info["width"], map_info["height"])
    if viewbox != target_viewbox:
        problems.append("SVG viewbox is {} but should be {}".format(viewbox, target_viewbox))
    dim_match(svg, map_info["width"], map_info["height"])
    ori_is_zero(image)
    dim_match(image, map_info["width"], map_info["height"])
    return problems


def load_svg(svg_data):
    tree = ElTree.fromstring(svg_data)
    parent_map = {c: p for p in tree.iter() for c in p}
    point_annotations = tree.findall(".//{}[@class='circle_annotation']".format(circle_el))
    point_names = tree.findall(".//{}[@class='circle_annotation']/../{}".format(circle_el, text_el))
    pose_annotations = tree.findall(".//{}[@class='pose_line_annotation']".format(line_el))
    pose_names = tree.findall(".//{}[@class='pose_line_annotation']/../{}".format(line_el, text_el))
    region_annotations = tree.findall(".//{}[@class='region_annotation']".format(poly_el))
    region_names = tree.findall(".//{}[@class='region_annotation']/../{}".format(poly_el, text_el))
    path_groups = tree.findall(".//{}[{}]".format(group_el, path_el))
    circle_groups = tree.findall(".//{}[{}]".format(group_el, circle_el))
    # The point annotations we care about have just a dot and a text label
    point_groups = filter(lambda g: len(list(g)) == 2, circle_groups)
    # Door annotations have a line, two circles and a text label
    door_groups = filter(lambda g: len(list(g)) == 4, circle_groups)

    point_parents = map(parent_map.__getitem__, point_annotations)
    points = process_point_annotations(point_names, point_annotations, point_parents)
    pose_parents = map(parent_map.__getitem__, pose_annotations)
    poses = process_pose_annotations(pose_names, pose_annotations, pose_parents)
    region_parents = map(parent_map.__getitem__, region_annotations)
    regions = process_region_annotations(region_names, region_annotations, region_parents)

    # NOTE(nickswalker): Haven't set a format for these in the annotation tool yet, so inkscape only assumption
    doors = process_door_groups(door_groups)

    # Messier extraction to get annotations stored as paths. These are from Inkscape or other regular editing tools.
    path_poses, path_regions = process_paths(path_groups)
    extra_points = []

    for group in point_groups:
        name = get_text_from_group(group).text
        try:
            translate = get_group_transform(group)
        except RuntimeError:
            warn("Can't process point group '{}' because it has a complex transform: {}".format(name, group.attrib[
                "transform"]))
            continue
        circle = group.find(".//{}".format(circle_el))

        if "class" in circle.attrib:
            # This was probably created by the annotation tool. Already processed above
            continue

        pixel_coord = float_s3(circle.attrib["cx"]) + translate[0], float_s3(circle.attrib["cy"]) + translate[1]
        extra_points.append((name, pixel_coord))

    points += extra_points
    poses += path_poses
    regions += path_regions

    return points, poses, regions, doors


def get_group_transform(group):
    if "transform" not in group.attrib:
        return 0, 0
    # We can only handle basic translate transforms right now
    translate_match = re.match(translation_pattern, group.attrib["transform"])
    if not translate_match:
        raise RuntimeError("Can't process because it has a complex transform: {}".format(group))
    else:
        return float_s3(translate_match.group(1)), float_s3(translate_match.group(2))


def get_text_from_group(group):
    # Inkscape tucks things in a tspan. Check that first
    text = group.find(".//{}".format(tspan_el))
    if text is None:
        text = group.find(".//{}".format(text_el))
    return text


def process_door_groups(door_groups):
    doors = []
    for door_group in door_groups:
        name = get_text_from_group(door_group).text
        translate = (0, 0)
        try:
            translate = get_group_transform(door_group)
        except RuntimeError:
            warn("Can't process door group '{}' because it has a complex transform: {}".format(name, door_group.attrib[
                "transform"]))
            continue
        approach_points = []
        circles = door_group.findall(circle_el)
        if len(circles) != 2:
            # Would we ever want more than 2 approach points?
            warn("Can't process door group '{}' because it had {} approach points (2 are expected)".format(name, len(
                circles)))
            continue
        for circle in circles:
            approach_points.append(
                (float_s3(circle.attrib["cx"]) + translate[0], float_s3(circle.attrib["cy"]) + translate[1]))
        try:
            door_line = extract_line_from_path(door_group.find(path_el), translate)
        except RuntimeError:
            warn("Couldn't extract line from door group '{}'".format(name))
            continue
        doors.append((name, door_line, approach_points))

    return doors


def is_line(path_part):
    from svgpathtools import Line
    return isinstance(path_part, Line)


def extract_line_from_path(path, translate=None):
    from svgpathtools import parse_path
    path_geom = parse_path(path.attrib["d"])
    if translate is None:
        translate = (0, 0)

    if len(path_geom) == 1 and is_line(path_geom[0]):
        line = path_geom[0]
        # We assume line starts at origin and points towards the second point
        start_coord = (float_s3(line.start.real) + translate[0], float_s3(line.start.imag) + translate[1])
        end_coord = (float_s3(line.end.real) + translate[0], float_s3(line.end.imag) + translate[1])
        return start_coord, end_coord
    else:
        raise RuntimeError()


def process_paths(path_groups):
    """
    Extracts pose and region annotations represented as paths

    :param path_groups: a list of groups each containing a text element and a path
    :return: a tuple of poses and regions
    """
    if len(path_groups) == 0:
        return [], []

    regions = []
    poses = []
    for group in path_groups:
        if len(list(group)) != 2:
            # May want to print a warning here
            continue

        # We assume that the text was created in inkscape so the string will be in a tspan
        path, text = group.find(".//{}".format(path_el)), get_text_from_group(group)
        if text is None:
            warn("No text label found for path group: {}".format(group))
            continue
        name = text.text

        translate = 0, 0
        try:
            translate = get_group_transform(group)
        except RuntimeError:
            warn("Can't process path group '{}' because it has a complex transform: {}".format(name, group.attrib[
                "transform"]))
            continue

        # Single line segment path => pose
        try:
            pose = extract_line_from_path(path, translate)
            poses.append(tuple([name] + list(pose)))
            continue
        except RuntimeError:
            pass

        # SVG paths are specified in a rich language of segment commands:
        # https://developer.mozilla.org/en-US/docs/Web/SVG/Attribute/d
        # We'll use a new dependency to extract what we can
        from svgpathtools import parse_path
        path_geom = parse_path(path.attrib["d"])
        # If they're all lines, let's assume it's closed and use it as a region
        if all(map(is_line, path_geom)):
            # Real part => x, imag part => y
            lines = map(lambda l: ((l.start.real, l.start.imag), (l.end.real, l.end.imag)), path_geom)
            # Each line segment starts where the previous ended, so we can drop the end points
            points = map(lambda l: l[0], lines)
            points = map(lambda p: (float_s3(p[0]), float_s3(p[1])), points)
            points = map(lambda p: (p[0] + translate[0], p[1] + translate[1]), points)
            regions.append((name, points))
        else:
            warn("Encountered path that couldn't be parsed {}".format(name))
    return poses, regions


def process_point_annotations(point_names, point_annotations, point_groups):
    points = []
    for point, text, parent in zip(point_annotations, point_names, point_groups):
        name = text.text
        translate = 0, 0
        try:
            translate = get_group_transform(parent)
        except RuntimeError:
            warn("Can't process point '{}' because it has a complex transform: {}".format(name,
                                                                                          parent.attrib["transform"]))
            continue
        pixel_coord = float_s3(point.attrib["cx"]) + translate[0], float_s3(point.attrib["cy"]) + translate[1]
        points.append((name, pixel_coord))
    return points


def process_pose_annotations(pose_names, pose_annotations, pose_groups):
    poses = []
    for pose, text, parent in zip(pose_annotations, pose_names, pose_groups):
        name = text.text
        translate = 0, 0
        try:
            translate = get_group_transform(parent)
        except RuntimeError:
            warn("Can't process pose '{}' because it has a complex transform: {}".format(name,
                                                                                         parent.attrib["transform"]))
            continue
        start_cord = float_s3(pose.attrib["x1"]) + translate[0], float_s3(pose.attrib["y1"]) + translate[1]
        stop_cord = float_s3(pose.attrib["x2"]) + translate[0], float_s3(pose.attrib["y2"]) + translate[1]
        poses.append((name, start_cord, stop_cord))
    return poses


def process_region_annotations(region_names, region_annotations, region_groups):
    regions = []
    for region, text, parent in zip(region_annotations, region_names, region_groups):
        name = text.text
        translate = 0, 0
        try:
            translate = get_group_transform(parent)
        except RuntimeError:
            warn("Can't process region '{}' because it has a complex transform: {}".format(name,
                                                                                           parent.attrib["transform"]))
            continue
        points_strs = region.attrib["points"].split()
        poly_points = [(float_s3(x_str), float_s3(y_str)) for x_str, y_str in map(lambda x: x.split(","), points_strs)]
        # Apply any translation
        poly_points = map(lambda p: (p[0] + translate[0], p[1] + translate[1]), poly_points)
        regions.append((name, poly_points))
    return regions


def transform_to_map_coords(map_info, points, poses, regions, doors):
    for i, point in enumerate(points):
        name, point = point
        point = point_to_map_coords(map_info, point)
        points[i] = (name, point)

    for i, pose in enumerate(poses):
        name, p1, p2 = pose
        p1 = point_to_map_coords(map_info, p1)
        p2 = point_to_map_coords(map_info, p2)
        poses[i] = (name, p1, p2)

    for i, region in enumerate(regions):
        name, poly_points = region
        poly_points = list(map(lambda p: point_to_map_coords(map_info, p), poly_points))
        regions[i] = (name, poly_points)

    for i, door in enumerate(doors):
        name, (d_p1, d_p2), (p1, p2) = door
        d_p1 = point_to_map_coords(map_info, d_p1)
        d_p2 = point_to_map_coords(map_info, d_p2)
        p1 = point_to_map_coords(map_info, p1)
        p2 = point_to_map_coords(map_info, p2)
        doors[i] = (name, (d_p1, d_p2), (p1, p2))

    return points, poses, regions, doors
