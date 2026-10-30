# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from __future__ import annotations

import json
import os as os
from typing import TYPE_CHECKING, Any, Callable, Dict, List

try:
    import hou  # type: ignore
except ImportError:  # pragma: no cover
    raise OSError("Could not find the Houdini module. Are you running this inside of Houdini?")

if TYPE_CHECKING:  # pragma: no cover
    from hou import Node, Parm, ParmTemplate


class HoudiniHandler:
    action_dict: Dict[str, Callable[[Dict[str, Any]], None]] = {}
    render_kwargs: Dict[str, Any]
    nodes: List[Node]

    def __init__(self) -> None:
        """
        Constructor for the houdini handler. Initializes action_dict and render variables
        """
        self.action_dict = {
            "scene_file": self.set_scene_file,
            "render_node": self.set_render_node,
            "ignore_input_nodes": self.set_ignore_input_nodes,
            "wedge_node": self.set_wedge_node,
            "wedgenum": self.set_wedge_num,
            "start_render": self.start_render,
        }
        self.render_kwargs = {"ignore_input_nodes": True}
        self.node = None
        self.wedge = None
        self.wegenum = None

    def _path_map_envs(self) -> None:
        """
        Applies the contents of HOUDINI_PATHMAP to global variables
        that are not automatically re-mapped.
        """

        # We know that $JOB and $POSE are not pathmapped automatically,
        # because they don't change when a scene file moves:
        # https://www.sidefx.com/docs/houdini/basics/project.html#tips-and-notes
        envs_to_remap: list[str] = ["JOB", "POSE"]

        for env in envs_to_remap:
            print(f"Mapping {env} ({hou.getenv(env)})")
            # Use the `pathmap` command to apply HOUDINI_PATHMAP to each variable
            # in case it missed mapping.
            # `-t` (test) applies the path mapping rules to the given path
            # `-c` (compact) prints out just the re-mapped path (with a newline we must strip)
            # https://www.sidefx.com/docs/houdini/commands/pathmap.html
            mapped_path, err = hou.hscript(f"pathmap -t '{hou.getenv(env)}' -c")
            if not err and mapped_path:
                hou.putenv(env, mapped_path.strip())
                print(f"Set {env} to {mapped_path.strip()}")
            elif err:
                print(f"Error mapping {env}: {err}")
            elif not mapped_path:
                print(f"Could not map {env}, no rules in HOUDINI_PATHMAP applied")

        # Reload the env variables
        hou.hscript("varchange")

    def _remap_lop_file_paths(self) -> None:
        """
        Applies HOUDINI_PATHMAP to LOP node file path parameters.

        HOUDINI_PATHMAP does not apply to USD/LOP nodes (sublayer, reference, etc.)
        because USD asset resolution uses ArResolver, not Houdini's file I/O layer.
        This method manually remaps any file reference string parms on LOP nodes.
        """
        if not os.environ.get("HOUDINI_PATHMAP"):
            return

        lop_nodes: list[Node] = hou.node("/").recursiveGlob("*", filter=hou.nodeTypeFilter.Lop)
        for node in lop_nodes:
            parms: list[Parm] = node.parms()
            for parm in parms:
                try:
                    template: ParmTemplate = parm.parmTemplate()
                    if template.type() != hou.parmTemplateType.String:
                        continue
                    if (
                        template.stringType() == hou.stringParmType.FileReference
                        or parm.name().startswith("filepath")
                    ):
                        original: str = parm.unexpandedString()
                        if not original or original.startswith("$") or original.startswith("`"):
                            continue
                        mapped, err = hou.hscript(f"pathmap -t '{original}' -c")
                        mapped = mapped.strip()
                        if err:
                            print(f"Error remapping {parm.path()}: {err}")
                        elif mapped and mapped != original:
                            parm.set(mapped)
                            print(f"Remapped LOP parm {parm.path()}: " f"{original} -> {mapped}")
                except hou.OperationFailed:
                    # Keyframed/animated parms have no single unexpanded string value, and
                    # calling parm.set() on them would clobber the animation channel, so skip.
                    # Report the resolved value so the log says which path will NOT be mapped.
                    try:
                        resolved = parm.evalAsString()
                    except hou.Error:
                        resolved = "<unresolved>"
                    print(
                        f"Skipping LOP parm {parm.path()} (keyframed): "
                        f"{resolved} will not be path-mapped"
                    )
                except hou.Error as exc:
                    # e.g. hou.PermissionError from parm.set() on a locked HDA parm. Skip this
                    # parm rather than aborting path mapping for the rest of the scene.
                    print(f"Skipping LOP parm {parm.path()}: {exc}")

    def _ods_pathmap_rules(self) -> list[tuple[str, str]]:
        """
        Parse HOUDINI_PATHMAP into (source, destination) prefix pairs, normalized
        to forward slashes. Used as a fallback when `pathmap -t` does not rewrite a
        path (e.g. an absolute Windows output path such as ``X:/...`` that husk will
        later write verbatim on the Linux worker).
        """
        raw = os.environ.get("HOUDINI_PATHMAP")
        if not raw:
            return []
        try:
            mapping = json.loads(raw)
        except (ValueError, TypeError):
            return []
        rules: list[tuple[str, str]] = []
        for src, dst in mapping.items():
            if not src or not dst:
                continue
            rules.append((str(src).replace("\\", "/"), str(dst).replace("\\", "/")))
        # Longest source first so the most specific prefix wins.
        rules.sort(key=lambda r: len(r[0]), reverse=True)
        return rules

    def _ods_map_output_path(self, path: str) -> str:
        """
        Map a render OUTPUT path to the worker filesystem.

        The DC Houdini adaptor already maps INPUT scene/LOP references, but the
        render output picture (the Karma RenderProduct ``productName`` / the ROP
        output image) is left as the authored absolute drive path (e.g. ``X:/...``).
        On a Linux worker that path is *relative*, so husk writes the frame to a
        throwaway ``./X:/...`` dir under the session and it is discarded at cleanup.

        This maps the path the same way inputs are mapped: try Houdini's own
        ``pathmap`` first, then fall back to a direct, separator-tolerant prefix
        replacement from HOUDINI_PATHMAP (handles the ``X:\\`` rule vs ``X:/`` path
        mismatch that defeats a naive prefix test).
        """
        if not path:
            return path

        # 1) Let Houdini try first.
        try:
            mapped, err = hou.hscript(f"pathmap -t '{path}' -c")
            mapped = mapped.strip()
            if not err and mapped and mapped != path:
                return mapped
        except hou.Error:
            pass

        # 2) Fallback: separator-tolerant prefix replacement from HOUDINI_PATHMAP.
        norm = path.replace("\\", "/")
        for src, dst in self._ods_pathmap_rules():
            src_trim = src.rstrip("/")
            if norm == src_trim or norm.startswith(src_trim + "/"):
                dst_trim = dst.rstrip("/")
                remainder = norm[len(src_trim):]
                result = dst_trim + remainder
                # Collapse any accidental double slash from a trailing-slash rule.
                while "//" in result[1:]:
                    result = result[0] + result[1:].replace("//", "/")
                return result
        return path

    def _remap_render_output_paths(self) -> None:
        """
        Map the render node's OUTPUT path parms onto the worker filesystem.

        Companion to ``_remap_lop_file_paths`` (which handles INPUT references).
        Without this, the Karma RenderProduct ``productName`` / the usdrender ROP
        output image keeps its authored ``X:/...`` drive path and the rendered EXR
        never lands on the shared project mount.

        Targets, per render-node type:
          * usdrender ROP: ``outputimage`` (output image override)
          * the Karma Render Settings LOP(s) feeding the ROP's ``loppath``:
            ``picture`` (output image), ``dcmfilename``, and ``productName`` when it
            holds a real output path.
        Internal USD scratch paths (``lopoutput``, e.g. ``__render__.usd``) are left
        alone -- they are session-local and must not be moved to the share.
        """
        if not os.environ.get("HOUDINI_PATHMAP") or not self.node:
            return

        output_parm_names = ("outputimage", "vm_picture", "soho_diskfile")
        self._ods_remap_node_output_parms(self.node, output_parm_names)

        # Follow the usdrender ROP's loppath to the render/karma settings LOPs and
        # remap their output picture parms too (that is where productName comes from).
        try:
            loppath_parm = self.node.parm("loppath")
        except hou.Error:
            loppath_parm = None
        if loppath_parm:
            lop = loppath_parm.evalAsNode()
            if lop:
                stage_parent = lop.parent()
                lop_output_parms = ("picture", "dcmfilename", "productName")
                # Remap the picture/product parms on every LOP under the same stage
                # network -- the render settings node may be upstream of loppath.
                nodes_to_check = {lop}
                if stage_parent:
                    nodes_to_check.update(
                        stage_parent.recursiveGlob("*", filter=hou.nodeTypeFilter.Lop)
                    )
                for n in nodes_to_check:
                    self._ods_remap_node_output_parms(n, lop_output_parms)

    def _ods_remap_node_output_parms(self, node: Node, parm_names: tuple[str, ...]) -> None:
        """Remap the named string output parms on ``node`` through the path map."""
        for name in parm_names:
            try:
                parm = node.parm(name)
            except hou.Error:
                parm = None
            if not parm:
                continue
            try:
                template = parm.parmTemplate()
                if template.type() != hou.parmTemplateType.String:
                    continue
                # Evaluate so that $HIP/$OS/etc. are resolved to a concrete path
                # (which may still be an unmapped X:/...), then map that.
                try:
                    original = parm.evalAsString()
                except hou.Error:
                    continue
                if not original:
                    continue
                # Only touch productName when it is a real image output path, not a
                # checkpoint/resume file that lives beside it.
                if name == "productName" and original.lower().endswith(".checkpoint"):
                    continue
                mapped = self._ods_map_output_path(original)
                if mapped and mapped != original:
                    parm.set(mapped)
                    print(f"Remapped output parm {parm.path()}: {original} -> {mapped}")
            except hou.OperationFailed:
                # Keyframed/expression parm; do not clobber the channel.
                print(f"Skipping output parm {node.path()}/{name} (keyframed/expression)")
            except hou.Error as exc:
                print(f"Skipping output parm {node.path()}/{name}: {exc}")

    def set_node_settings(self, node):
        # this is a place holder function
        # TODO remove after common node library implemented

        node_type = node.type().nameWithCategory().split("/")

        if node_type[0] != "Driver":
            return

        if len(node_type) < 2:
            return

        if node_type[1] == "ifd":
            # Mantra render node
            alfredProgress = node.parm("vm_alfprogress")
            if alfredProgress is not None:
                alfredProgress.set(1)
                print("Enabled Alfred style progress")
            verbosity = node.parm("vm_verbose")
            if verbosity is not None:
                # Mantra verbosity is an int with range 0 to 5
                if isinstance(verbosity.eval(), int) and verbosity.eval() < 2:
                    # 2 provides basic logging, we set it as a minimum to help with debugging issues
                    verbosity.set(2)
                    if verbosity.eval() == 2:
                        # The verbosity won't be changed if the parameter is "keyed", so we check the value before
                        # logging that it was increased
                        # https://www.sidefx.com/docs/houdini/network/parms.html#color
                        print("Increased verbosity to 2 to include basic logging")
                print(f"Logging verbosity is set to {verbosity.eval()}")
            return

        if node_type[1] == "usdrender":
            # Karma render node
            alfredProgress = node.parm("alfprogress")
            if alfredProgress is not None:
                alfredProgress.set(1)
                print("Enabled Alfred style progress")
            verbosity = node.parm("verbosity")
            if verbosity is not None:
                # Karma verbosity is a str with options "", "3", "9", "9p", "9P"
                # 3 means "Rendering Statistics", we set it as a minimum to help with debugging issues
                if isinstance(verbosity.eval(), str) and verbosity.eval() == "":
                    verbosity.set("3")
                    if verbosity.eval() == "3":
                        # The verbosity won't be changed if the parameter is "keyed", so we check the value before
                        # logging that it was increased
                        # https://www.sidefx.com/docs/houdini/network/parms.html#color
                        print("Increased verbosity to '3' to include basic logging")
                print(f"Logging verbosity is set to '{verbosity.eval()}'")

    def start_render(self, data: dict) -> None:
        """
        Uses active node and calls hou's render

        Args:
            data (dict):

        Raises:
            RuntimeError: .
        """

        def setenvvariable(var, val):
            hou.hscript(f"set {var} = {val}")
            hou.hscript("varchange")

        if not self.node:
            raise TypeError("Render node is 'None', no render node has been loaded")

        self.set_node_settings(self.node)

        # handle wedge data
        if self.wedge and self.wedgenum:
            print("Wedged step")
            wedgenum = int(self.wedgenum)
            hm = self.wedge.hdaModule()
            allwedge, _stashedparms, _errormsg = hm.getwedges(self.wedge)
            if 0 <= wedgenum < len(allwedge):
                wl = allwedge[wedgenum]
                setenvvariable("WEDGENUM", str(wedgenum))
                print(f"Applying wedge: {wedgenum}")
                hm.applyspecificwedge(self.wedge, wl)
            else:
                raise ValueError(f"WEDGENUM out of range: {wedgenum}")

        self.node.parm("trange").set(1)

        # Map the render OUTPUT path(s) onto the worker filesystem. Inputs are
        # already mapped (set_scene_file -> _remap_lop_file_paths); without this the
        # output picture keeps its authored X:/... drive path and husk writes the
        # frame to a throwaway ./X:/... dir on Linux that is discarded at cleanup.
        self._remap_render_output_paths()

        interleave = hou.renderMethod.RopByRop

        start = data["frame_range"]["start"]
        end = data["frame_range"]["end"]
        step = data["frame_range"]["step"]

        self.node.render(
            verbose=True,
            frame_range=(start, end, step),
            ignore_inputs=self.render_kwargs["ignore_input_nodes"],
            method=interleave,
        )

        setenvvariable("WEDGENUM", "")

        print("Finished Rendering")

    def set_ignore_input_nodes(self, data: dict) -> None:
        """
        Sets
        Args:
            data (dict): ]
        """
        self.render_kwargs["ignore_input_nodes"] = bool(data.get("ignore_input_nodes", True))

    def set_render_node(self, data: dict) -> None:
        """
        Sets the render node to render

        Args:
            data (dict):
        """
        node = hou.node(data.get("render_node", ""))
        if node is None:
            raise TypeError("Render node is 'None', no render node has been loaded")
        print(f"node: {node}")
        self.node = node

    def set_wedge_node(self, data: dict) -> None:
        """
        sets the wedge node

        args:
            data (dict):
        """
        wedge_node_path = data.get("wedge_node", "")
        if wedge_node_path:
            wedge = hou.node(wedge_node_path)
            if wedge is not None:
                print(f"wedge node: {wedge}")
                self.wedge = wedge

    def set_wedge_num(self, data: dict) -> None:
        """
        sets the wedge num

        args:
            data (dict):
        """
        wedgenum = data.get("wedgenum", None)
        if wedgenum is not None:
            print(f"wedgenum {wedgenum}")
            self.wedgenum = wedgenum

    def set_scene_file(self, data: dict) -> None:
        """
        Opens the scene file in Houdini.

        Args:
            data (dict): The data given from the Adaptor. Keys expected: ['scene_file']

        Raises:
            FileNotFoundError: If path to the scene file does not yield a file
        """
        scene_path = data.get("scene_file", "")
        if not os.path.isfile(scene_path):
            raise FileNotFoundError(f"The scene file '{scene_path}' does not exist")
        try:
            hou.hipFile.load(scene_path)
        except hou.LoadWarning as e:
            print(e)

        self._path_map_envs()
        self._remap_lop_file_paths()
