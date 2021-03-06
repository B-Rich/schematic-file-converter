#!/usr/bin/env python2
""" The Gerber RS274-X Format Parser """

# upconvert.py - A universal hardware design file format converter using
# Format:       upverter.com/resources/open-json-format/
# Development:  github.com/upverter/schematic-file-converter
#
# Copyright 2011 Upverter, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import re
from math import sqrt, sin, cos, acos, pi
from collections import namedtuple
from zipfile import ZipFile
from tarfile import TarFile, ReadError
import csv
from os import path

from upconvert.core.design import Design
from upconvert.core.layout import Layout, Layer, Image, Macro, Primitive
from upconvert.core.layout import Fill, Smear, ShapeInstance, Aperture
from upconvert.core.shape import Line, Arc, Point, Circle, Rectangle
from upconvert.core.shape import Obround, RegularPolygon, Polygon, Moire, Thermal


# exceptions

class Unparsable(ValueError):
    """ Superclass for all parser errors. """

class ParamError(Unparsable):
    """ Superclass for parameter errors. """

class CoordError(Unparsable):
    """ Superclass for coordinate errors. """

class DelimiterMissing(ParamError):
    """ Missing paramater block delimiter (%). """

class ParamContainsBadData(ParamError):
    """ Non-paramater found within param block. """

class CoordPrecedesFormatSpec(CoordError):
    """ Coordinate data prior to format specification. """

class CoordMalformed(CoordError):
    """ Coordinate block doesn't conform to spec. """

class QuadrantViolation(CoordError):
    """ Single quadrant arc longer than 0.5 radians/pi. """

class OpenFillBoundary(CoordError):
    """ Fill boundary ends do not equate. """

class FileNotTerminated(Unparsable):
    """ M02* was not encountered. """

class DataAfterEOF(Unparsable):
    """ M02* was not the last thing in the file. """

class UnintelligibleDataBlock(Unparsable):
    """ Data block did not conform to any known pattern. """

class IncompatibleAperture(Unparsable):
    """ Attempted to draw non-linear shape with rect. """

class InvalidExpression(Unparsable):
    """ Invalid macro expression. """


# token classes

LayerDef = namedtuple('LayerDef', 'name type filename')             # pylint: disable=C0103
MacroDef = namedtuple('MacroDef', 'name primitive_defs')            # pylint: disable=C0103
Coord = namedtuple('Coord', 'x y i j')                              # pylint: disable=C0103
CoordFmt = namedtuple('CoordFmt', 'int dec')                        # pylint: disable=C0103
AxisDef = namedtuple('AxisDef', 'a b')                              # pylint: disable=C0103
Funct = namedtuple('Funct', 'type_ code')                           # pylint: disable=C0103
FormatSpec = namedtuple('FormatSpec', ['zero_omission',             # pylint: disable=C0103
                        'incremental_coords', 'n_max', 'g_max',
                        'x', 'y', 'd_max', 'm_max'])
Token = namedtuple('Token', 'type value')                           # pylint: disable=C0103


# constants

DEBUG = False
LAYERS_CFG = 'layers.cfg'
PRIMITIVES = {1:'circle',
              2:'line-vector',
              20:'line-vector',
              21:'line-center',
              22:'line-lower-left',
              4:'outline',
              5:'polygon',
              6:'moire',
              7:'thermal'}
D_MAP = {1:'ON', 2:'OFF', 3:'FLASH'}
G_MAP = {1:{'interpolation':'LINEAR'},
         2:{'interpolation':'CLOCKWISE_CIRCULAR'},
         3:{'interpolation':'ANTICLOCKWISE_CIRCULAR'},
         36:{'outline_fill':True},
         37:{'outline_fill':False},
         70:{'units':'IN'},
         71:{'units':'MM'},
         74:{'multi_quadrant':False},
         75:{'multi_quadrant':True},
         90:{'incremental':True},
         91:{'incremental':False}}
IMAGE_POLARITIES = {'C': False,
                    'D': True}
AXIS_PARAMS = ('AS', 'MI', 'OF', 'SF', 'IJ', 'IO')
ALL_PARAMS = ('AS', 'FS', 'MI', 'MO', 'OF', 'SF',
              'IJ', 'IN', 'IO', 'IP', 'IR', 'PF', 'AD',
              'KO', 'LN', 'LP', 'SR')
LAYER_PARAMS = ALL_PARAMS[-4:]
TOK_SPEC = (('MACRO', r'%AM[^%]*%'),
            ('PARAM_DELIM', r'%'),
           ) + tuple([(p, r'%s[^\*]*\*' % p) for p in ALL_PARAMS]) + (
            ('COMMENT', r'G04[^\*]*\*'),
            ('DEPRECATED', r'G54\*?'), # historic crud
            ('FUNCT', r'[GD][^\*]*\*'),# function codes
            ('COORD', r'[XY][^\*]*\*'),# coordinates
            ('EOF', r'M02\*'),         # end of file
            ('SKIP', r'M0[01]\*|N[^\*]*\*|\**\s+'), # historic crud, whitespace
            ('UNKNOWN', r'[^\*]*\*'))  # unintelligble data block
IGNORE = ('SKIP', 'COMMENT', 'DEPRECATED')
REG_EX = '|'.join('(?P<%s>%s)' % pair for pair in TOK_SPEC)
TOK_RE = re.compile(REG_EX, re.MULTILINE)


# parser

class Gerber:
    """ The Gerber Format Parser """

    def __init__(self, ignore_unknown=True):
        self.ignore_unknown = ignore_unknown
        self.layout = Layout()
        self.layer_buff = None
        self.macro_buff = None
        self.img_buff = Image()
        self.trace_buff = TraceBuffer()
        self.fill_buff = []

        # establish gerber defaults
        self.params = {'AS':AxisDef('x', 'y'),# axis select
                       'FS':None,             # format spec
                       'MI':AxisDef(0, 0),    # mirror image
                       'MO':'IN',             # mode: inches/mm
                       'OF':AxisDef(0, 0),    # offset
                       'SF':AxisDef(1, 1),    # scale factor
                       'IJ':AxisDef(('L', 0), # image justify
                                    ('L', 0)),
                       'IO':AxisDef(0, 0),    # image offset
                       'IP':True,             # image polarity
                       'IR':0}                # image rotation

        # simulate a photo plotter
        self.status = {'x':0,
                       'y':0,
                       'draw':'OFF',
                       'interpolation':'LINEAR',
                       'aperture':None,
                       'outline_fill':False,
                       'multi_quadrant':False,
                       'units':None,
                       'incremental_coords':None}


    @staticmethod
    def auto_detect(filename):
        """ Return our confidence that the given file is an gerber file """
        with open(filename, 'r') as f:
            data = f.read(4096)
        confidence = 0
        if '%ADD' in data:
            confidence += 0.2
        if 'D01*' in data:
            confidence += 0.2
        if 'D02*' in data:
            confidence += 0.2
        if 'D03*' in data:
            confidence += 0.2
        if 'M02*' in data:
            confidence += 0.2
        if filename.endswith('.ger'):
            confidence += 0.5
        return confidence


    def parse(self, infile='.'):
        """ Parse tokens from gerber files into a design. """
        is_zip = infile.endswith('.zip')
        openarchive = ZipFile if is_zip else TarFile.open
        archive = batch_member = None
        try:
            # define multiple layers from folder
            if LAYERS_CFG in infile:
                archive = None
                cfg_name = infile
                cfg = open(cfg_name, 'r')

            # define multiple layers from archive
            else:
                archive = openarchive(infile)
                batch = archive.namelist if is_zip else archive.getnames
                batch_member = archive.open if is_zip else archive.extractfile
                cfg_name = [n for n in batch() if LAYERS_CFG in n][0]
                cfg = batch_member(cfg_name)

        # define single layer from single gerber file
        except ReadError:
            name, ext = path.split(infile)[1].rsplit('.', 1)
            layer_defs = [LayerDef(ext.lower() == 'ger' and name or ext,
                                   'unknown', infile)]
            self._gen_layers(layer_defs, None, None)

        # tidy up batch specs
        else:
            layer_defs = [LayerDef(rec[0],
                                   rec[1],
                                   path.join(path.split(cfg_name)[0], rec[2]))
                          for rec in
                          csv.reader(cfg, skipinitialspace=True)]
            cfg.close()
            self._gen_layers(layer_defs, archive, batch_member)

        # tidy up archive
        finally:
            if archive:
                archive.close()

        # compile design
        if DEBUG:
            self._debug_stdout()

        self.layout.units = (self.params['MO'] == 'IN' and 'inch' or 'mm')

        design = Design()
        design.layout = self.layout
        return design


    # primary parser support methods

    def _gen_layers(self, layer_defs, archive, batch_member):
        """ Parse gerbers into a PCB layers. """
        for layer_def in layer_defs:
            self.layer_buff = Layer(layer_def.name, layer_def.type)
            self.macro_buff = {}
            layer_file = (archive and
                          batch_member(layer_def.filename) or
                          open(layer_def.filename, 'r'))
            for block in self._tokenize(layer_file):
                if isinstance(block, MacroDef):
                    self.macro_buff[block.name] = InternalMacro(block)
                    effect = {}
                elif isinstance(block, Funct):
                    effect = self._do_funct(block)
                else:
                    effect = self._move(block)
                self.status.update(effect)
            self.layout.layers.append(self.layer_buff)


    def _do_funct(self, block):
        """ Set drawing modes, fill terminators. """
        code = int(block.code)
        if 'D' in block.type_:
            if code < 10:
                effect = {'draw':D_MAP[code]}

                # flash current pos/aperture
                if 'X' in block.type_:
                    apertures = self.layer_buff.apertures
                    aperture = apertures[self.status['aperture']]
                    pos = Point(self.status['x'], self.status['y'])
                    shape_inst = ShapeInstance(pos, aperture)
                    self.img_buff.shape_instances.append(shape_inst)

                # terminate fill mid mode
                if (self.status['outline_fill'] and
                    code == 2 and self.fill_buff):
                    self.img_buff.fills.append(self._check_fill())

            else:
                effect = {'aperture':block.code}
        else:
            effect = G_MAP[code]
            if code == 37:

                # terminate fill if D02 was not specified
                if self.fill_buff:
                    self.img_buff.fills.append(self._check_fill())

        return effect


    def _move(self, block):
        """ Draw a shape, or a segment of a trace or fill. """
        start = tuple([self.status[k] for k in ('x', 'y')])
        end = self._target_pos(block)
        ends = (Point(start), Point(end))
        apertures = self.layer_buff.apertures

        if self.status['draw'] == 'ON':
            # generate segment
            if self.status['interpolation'] == 'LINEAR':
                seg = Line(start, end)
            else:
                ctr_offset = block[2:]
                seg = self._draw_arc(ends, ctr_offset)

            # append segment to fill
            if self.status['outline_fill']:
                self.fill_buff.append((ends, seg))
            else:
                aperture = apertures[self.status['aperture']]
                if isinstance(aperture.shape, Rectangle):
                    # construct a smear
                    self._check_smear(seg, aperture.shape)
                    self.img_buff.smears.append(Smear(seg, aperture.shape))
                else:
                    wid = aperture.shape.radius * 2
                    trace = self.trace_buff.get_trace(wid, seg)
                    if trace is None:
                        # FIXME(shamer): fill into segments not old Trace class
                        #trace = Trace(wid)
                        self.img_buff.traces.append(trace)
                    trace.segments.append(seg)
                    self.trace_buff.add_segment(seg, trace)

        elif self.status['draw'] == 'FLASH':
            aperture = apertures[self.status['aperture']]
            shape_inst = ShapeInstance(ends[1], aperture)
            self.img_buff.shape_instances.append(shape_inst)

        return {'x':end[0], 'y':end[1]}


    # coordinate interpretation

    def _target_pos(self, block):
        """ Interpret coordinates in a data block. """
        coord = {'x':block.x, 'y':block.y}
        for k in coord:
            if self.params['FS'].incremental_coords:
                if coord[k] is None:
                    coord[k] = 0
                coord[k] = self.status[k] + getattr(block, k)
            else:
                if coord[k] is None:
                    coord[k] = self.status[k]
        return (coord['x'], coord['y'])


    # geometry

    def _draw_arc(self, end_pts, center_offset):
        """ Convert arc path into shape. """
        start, end = end_pts
        offset = {'i':center_offset[0], 'j':center_offset[1]}
        for k in offset:
            if offset[k] is None:
                offset[k] = 0
        center, radius = self._get_ctr_and_radius(end_pts, offset)
        start_angle = self._get_angle(center, start)
        end_angle = self._get_angle(center, end)
        clockwise = 'ANTI' not in self.status['interpolation']
        self._check_mq(start_angle, end_angle, clockwise)
        return Arc(center.x, center.y,
                   start_angle if clockwise else end_angle,
                   end_angle if clockwise else start_angle,
                   radius)


    def _get_ctr_and_radius(self, end_pts, offset):
        """ Apply gerber circular interpolation logic. """
        start, end = end_pts
        radius = sqrt(offset['i']**2 + offset['j']**2)

        centers = [Point(start.x + xsign * offset['i'],
                         start.y + ysign * offset['j'])
                   for xsign in (1, -1) for ysign in (1, -1)]

        center = min(centers, key=lambda c: abs(c.dist(end) - radius))

        return (center, radius)


    def _get_angle(self, arc_center, point):
        """
        Convert 2 points to an angle in radians/pi.

        0 radians = 3 o'clock, in accordance with
        the way arc angles are defined in shape.py

        """
        adj = float(point.x - arc_center.x)
        opp = point.y - arc_center.y
        hyp = arc_center.dist(point)
        if hyp == 0.0:
            return 0.0
        angle = acos(adj/hyp)/pi
        if opp > 0:
            angle = 2 - angle
        return angle


    # tokenizer

    def _tokenize(self, layer_file):
        """ Split gerber file into pythonic tokens. """
        content = layer_file.read()
        layer_file.close()
        param_block = eof = False
        pos = 0
        match = TOK_RE.match(content)
        while pos < len(content):
            if match is None:
                pos += 1
            else:
                typ = match.lastgroup
                tok = match.group(typ)[:-1]
                try:
                    if typ == 'MACRO':
                        yield self._parse_macro(tok)
                    elif typ == 'PARAM_DELIM':
                        param_block = not param_block

                    # params
                    elif len(typ) == 2:
                        self._check_pb(param_block, tok)
                        self.params.update(self._parse_param(tok))

                    # data blocks
                    elif typ not in IGNORE:
                        if typ == 'EOF':
                            self._check_eof(content[match.end():])
                            eof = True
                        elif typ == 'UNKNOWN':
                            if not self.ignore_unknown:
                                raise UnintelligibleDataBlock(tok)
                        else:
                            self._check_pb(param_block, tok, False)
                            # explode self-referential data blocks
                            blocks = self._parse_data_block(tok)
                            for block in blocks:
                                yield block

                except Unparsable:
                    raise
                pos = match.end()
            match = TOK_RE.match(content, pos)
        self._check_eof(eof=eof)
        self.layer_buff.images.append(self.img_buff)


    # tokenizer support - macros

    def _parse_macro(self, tok):
        """ Define a macro, with its component shapes. """
        parts = [part.strip() for part in tok.split('*')]
        name = parts[0][3:]
        prims =  [part.split(',') for part in parts[1:-1] if part]
        int_prims = []

        for prim in prims:
            if prim[0].startswith('$'):
                shape_type = 'ignore'
                mods = prim
            else:
                shape_type = PRIMITIVES[int(prim[0])]
                mods = prim[1:]

            int_prims.append(InternalPrimitive(shape_type, mods))

        return MacroDef(name, int_prims)


    # tokenizer support - params

    def _parse_param(self, tok):
        """ Convert a param specifier into pythonic data. """
        name, tok = (tok[:2], tok[2:])
        if name == 'FS':
            tup = self._extract_fs(tok)
        elif name == 'AD':
            self._extract_ad(tok)
            return {}
        elif name in AXIS_PARAMS:
            tup = self._extract_ap(name, tok)
        elif name in LAYER_PARAMS:
            self._extract_lp(name, tok)
            return {}
        else:
            tup = tok
        return {name:tup}


    def _extract_fs(self, tok):
        """ Extract format spec param parts into tuple. """
        tok, m_max = self._pop_val('M', tok)
        tok, d_max = self._pop_val('D', tok)
        tok, y = self._pop_val('Y', tok, coerce_=False)
        y = CoordFmt(int(y[0]), int(y[1]))
        tok, x = self._pop_val('X', tok, coerce_=False)
        x = CoordFmt(int(x[0]), int(x[1]))
        tok, g_max = self._pop_val('G', tok)
        tok, n_max = self._pop_val('N', tok)
        inc_coords = ('I' in tok)
        z_omit = tok[0]
        return FormatSpec(z_omit, inc_coords, n_max, g_max,
                          x, y, d_max, m_max)


    def _extract_ad(self, tok):
        """ Extract aperture definition into shapes dict. """
        tok = tok if ',' in tok else tok + ','
        code_end = 4 if tok[3].isdigit() else 3
        code = tok[1:code_end]
        ap_type, mods = tok[code_end:].split(',')
        if mods:
            mods = [float(m) for m in mods.split('X') if m]

        # An aperture can use any of the 4 standard types,
        # (with or without a central hole), or a previously
        # defined macro.
        if ap_type == 'C':
            shape = Circle(0, 0, mods[0]/2)
            hole_defs = len(mods) > 1 and mods[1:]
        elif ap_type == 'R':
            if len(mods) == 1:
                shape = Rectangle(-mods[0]/2, mods[0]/2,
                                   mods[0], mods[0])
            else:
                shape = Rectangle(-mods[0]/2, mods[1]/2,
                                   mods[0], mods[1])
            hole_defs = len(mods) > 2 and mods[2:]
        elif ap_type == 'O':
            shape = Obround(0, 0, mods[0], mods[1])
            hole_defs = len(mods) > 2 and mods[2:]
        elif ap_type == 'P':
            if len(mods) < 3:
                mods.append(0)
            shape = RegularPolygon(0, 0, mods[0], mods[1], mods[2])
            hole_defs = len(mods) > 3 and mods[3:]
        else: # macro
            shape = ap_type
            if shape in self.macro_buff:
                macro = self.macro_buff[shape].instantiate(mods)
                counter = 0 # pick a unique name for the macro
                while mods and macro.name in self.layer_buff.macros:
                    macro.name = shape + str(counter)
                    counter += 1
                self.layer_buff.macros[macro.name] = macro
            hole_defs = None

        if hole_defs and (len(hole_defs) > 1):
            hole = Rectangle(-hole_defs[0]/2,
                              hole_defs[1]/2,
                              hole_defs[0],
                              hole_defs[1])
        elif hole_defs:
            hole = Circle(0, 0, hole_defs[0]/2)
        else:
            hole = None

        self.layer_buff.apertures.update({code:Aperture(code, shape, hole)})


    def _extract_ap(self, name, tok):
        """ Extract axis-defining param into tuple. """
        if name in ('AS', 'IJ'):
            coerce_ = False
        else:
            coerce_ = name == 'MI' and 'int' or 'float'
        tok, b_val = self._pop_val('B', tok, coerce_=coerce_)
        tok, a_val = self._pop_val('A', tok, coerce_=coerce_)
        if name == 'IJ':
            tup = AxisDef(self._parse_justify(a_val),
                          self._parse_justify(b_val))
        else:
            tup = AxisDef(a_val, b_val)
        return tup


    def _extract_lp(self, name, tok):
        """ Extract "layer param" and reset image layer. """
        if self.img_buff.not_empty():
            self.layer_buff.images.append(self.img_buff)
            self.img_buff = Image('', self.img_buff.is_additive)
            self.trace_buff = TraceBuffer()
            self.status.update({'x':0,
                                'y':0,
                                'draw':'OFF',
                                'interpolation':'LINEAR',
                                'aperture':None,
                                'outline_fill':False,
                                'multi_quadrant':False,
                                'units':None,
                                'incremental_coords':None})
        if name == 'LN':
            self.img_buff.name = tok
        elif name == 'LP':
            self.img_buff.is_additive = IMAGE_POLARITIES[tok]
        elif name == 'SR':
            tok, j = self._pop_val('J', tok, coerce_='float')
            tok, i = self._pop_val('I', tok, coerce_='float')
            tok, y = self._pop_val('Y', tok)
            tok, x = self._pop_val('X', tok)
            self.img_buff.x_repeats = x
            self.img_buff.x_step = i
            self.img_buff.y_repeats = y
            self.img_buff.y_step = j


    def _parse_justify(self, val):
        """ Make a tuple for each axis (special case). """
        if len(val) > 1 or val not in 'LC':
            ab_tup = ('L', float(val.split('L')[-1]))
        else:
            ab_tup = (val, None)
        return ab_tup


    # tokenizer support - funct/coord

    def _parse_data_block(self, tok):
        """ Convert a non-param into pythonic data. """
        if 'G' in tok:
            g_code = tok[1:3]
            tok = tok[3:]
            if int(g_code) in G_MAP:
                yield Funct('G', g_code)
        tok, d_code = self._pop_val('D', tok, coerce_=False)
        if d_code:

            # identify D03 without coord - flash at current pos
            type_ = (d_code == '03' and not tok) and 'XD' or 'D'

            yield Funct(type_, d_code)
        if tok:
            yield self._parse_coord(tok)


    def _parse_coord(self, tok):
        """ Convert a coordinate set into pythonic data. """
        self._check_fs()
        tok, j = self._pop_val('J', tok, format_=True)
        tok, i = self._pop_val('I', tok, format_=True)
        tok, y = self._pop_val('Y', tok, format_=True)
        tok, x = self._pop_val('X', tok, format_=True)
        result = Coord(x, y, i, j)
        if tok:
            raise CoordMalformed('%s remainder=%s' % (result, tok))
        return result


    def _format_dec(self, num_str, axis):
        """
        Interpret a coordinate value using format spec.

        Params: num_str (the string representation of a number
                         portended by format spec)
                axis (X or Y - not to be confused with A/B)

        Returns: float

        """
        f_spec = self.params['FS']
        sign_wid = num_str[0] in ('-', '+') and 1 or 0
        int_wid = sign_wid + f_spec[axis].int
        wid = int_wid + f_spec[axis].dec

        # pad coordinate to specified width
        if f_spec.zero_omission == 'L':
            num_str = num_str.zfill(wid)
        elif f_spec.zero_omission == 'T':
            num_str = num_str.rjust(wid, '0')
        if len(num_str) != wid:
            raise CoordMalformed('num_str: %s wid: %s' % (num_str, wid))

        # insert decimal point
        num_str = '%s.%s' % (num_str[:int_wid], num_str[int_wid:])

        return float(num_str)


    # general support methods

    def _pop_val(self, key, tok, format_=False, coerce_='int'):
        """ Pop a labelled value from the end of a token. """
        val = None
        if key in tok:
            tok, num_str = tok.split(key)
            if num_str:
                if format_:
                    if key in ('X', 'I'):
                        val = self._format_dec(num_str, 4)
                    else:
                        val = self._format_dec(num_str, 5)
                else:
                    if coerce_ == 'float':
                        if '.' in num_str:
                            val = int(float(num_str))
                        else:
                            val = int(num_str)
                    elif coerce_:
                        val = int(num_str)
                    else:
                        val = num_str

        return (tok, val)


    def _check_fill(self):
        """ Check that a fill is closed. """
        ends = [pair[0] for pair in self.fill_buff]
        fill = [pair[1] for pair in self.fill_buff]
        self.fill_buff = []

        if ends[0][0] == ends[-1][1]:
            return Fill(fill)
        else:
            raise OpenFillBoundary('%s != %s' % (ends[-1][1], ends[0][0]))


    def _check_smear(self, seg, shape):
        """ Enforce linear interpolation constraint. """
        if not isinstance(seg, Line):
            raise IncompatibleAperture('%s cannot draw arc %s' % (shape, seg))


    def _check_mq(self, start_angle, end_angle, clockwise):
        """ Enforce single quadrant arc length restriction. """

        if not self.status['multi_quadrant']:
            if clockwise:
                arc = (2.0 if end_angle == 0.0 else end_angle) - \
                    (0.0 if start_angle == 2.0 else start_angle)
            else:
                arc = (2.0 if start_angle == 0.0 else start_angle) - \
                    (0.0 if end_angle == 2.0 else end_angle)

            if round(abs(arc), 4) > 0.5:
                raise QuadrantViolation('Arc(%s to %s) > 0.5 rad/pi'
                                        % (start_angle, end_angle))


    def _check_pb(self, param_block, tok, should_be=True):
        """ Ensure we are parsing an appropriate block. """
        if should_be and not param_block:
            raise DelimiterMissing
        if param_block and not should_be:
            raise ParamContainsBadData(tok)


    def _check_fs(self):
        """ Ensure coordinate is able to be interpreted. """
        if not self.params['FS']:
            raise CoordPrecedesFormatSpec


    def _check_eof(self, trailing_fragment=None, eof=False):
        """ Ensure file is terminated correctly. """
        if not (trailing_fragment or eof):
            raise FileNotTerminated
        elif (not eof) and trailing_fragment.strip():
            raise DataAfterEOF(trailing_fragment)


    def _debug_stdout(self):
        """ Dump what we know. """
        for layer in self.layout.layers:
            print '-- %s (%s) --' % (layer.name, layer.type)
            for j in layer.apertures:
                print '-- D%s --' % j
                print layer.apertures[j].json()
            for k in layer.macros:
                print '-- %s --' % k
                print layer.macros[k].json()
            for image in layer.images:
                print '-- %s (%s) --' % (image.name,
                                         image.is_additive and
                                         'additive' or 'subtractive')
                print image.json()
        raise Unparsable('deliberate error')


class InternalMacro(object):
    """
    Complex shape built from multiple primitives.

    Primitive shapes are added together in the order they appear in
    the list. Subtractive shapes subtract only from prior shapes, not
    subsequent shapes.
    """

    def __init__(self, block):
        self.name = block.name
        self.primitives = block.primitive_defs

    def instantiate(self, values):
        """ Return a core.layout.Macro given the list of values
        provided in the aperture definition."""

        values = dict(enumerate(values, 1))
        return Macro(self.name, filter(None, [p.instantiate(values)
                                              for p in self.primitives]))


class InternalPrimitive(object):
    """ A shape with modifiers. """

    def __init__(self, shape_type, modifiers):
        self.shape_type = shape_type # string, e.g., ('line', 'rectangle', etc.)
        self.modifiers = [Modifier(m) for m in modifiers]


    def instantiate(self, values):
        """ Return a core.layout.Primitive with a set of fixed shapes
        given a dict mapping variable numbers to values, or None if there
        is no corresponding shape. """

        shape_type = self.shape_type

        mods = [m.evaluate(values) for m in self.modifiers]

        if shape_type == 'ignore':
            return None

        is_additive = True if shape_type in ('moire', 'thermal') \
            else bool(mods[0])

        rotation = 0 if shape_type in ('circle', 'moire', 'thermal') \
            else mods[-1]/180

        if shape_type == 'circle':
            shape = Circle(x=mods[2],
                           y=mods[3],
                           radius=mods[1]/2)
        elif shape_type == 'line-vector':
            shape, rotation = self._vector_to_rect(mods, rotation)
        elif shape_type == 'line-center':
            shape = Rectangle(x=mods[3] - mods[1]/2,
                              y=mods[4] + mods[2]/2,
                              width=mods[1],
                              height=mods[2])
        elif shape_type == 'line-lower-left':
            shape = Rectangle(x=mods[3],
                              y=mods[4] + mods[2],
                              width=mods[1],
                              height=mods[2])
        elif shape_type == 'outline':
            points = [Point(mods[i], mods[i + 1])
                      for i in range(2, len(mods[:-1]), 2)]
            shape = Polygon(points)
        elif shape_type == 'polygon':
            shape = RegularPolygon(x=mods[2],
                                   y=mods[3],
                                   outer=mods[4],
                                   vertices=mods[1])
        elif shape_type == 'moire':
            mods[8] = 2 - mods[8]/180
            shape = Moire(*mods[0:9])
        elif shape_type == 'thermal':
            mods[5] = 2 - mods[5]/180
            shape = Thermal(*mods[0:6])

        return Primitive(is_additive, rotation, shape)


    def _vector_to_rect(self, mods, rotation):
        """
        Convert a vector into a Rectangle.

        Strategy
        ========
        If vect is not horizontal:
            - rotate about the origin until horizontal
            - define it as a normal rectangle
            - incorporate rotated angle into explicit rotation

        """
        start, end = (mods[2:4], mods[4:6])
        start_radius = sqrt(start[0]**2 + start[1]**2)
        end_radius = sqrt(end[0]**2 + end[1]**2)

        # Reverse the vector if its endpoint is closer
        # to the origin than its start point (avoids
        # mucking about with signage later).
        if start_radius > end_radius:
            end, start = (mods[2:4], mods[4:6])
            radius = end_radius
        else:
            radius = start_radius

        # Calc the angle of the vector with respect to the x axis.
        x, y = start
        adj = end[0] - x
        opp = end[1] - y
        hyp = sqrt(adj**2 + opp**2)
        theta = acos(adj/hyp)/pi
        if opp > 0:
            theta = 2 - theta

        # Represent vector angle as a delta.
        d_theta = 2 - theta

        # Calc the angle of the start point of the flattened vector.

        theta = 0.0 if radius == 0.0 else acos(x/radius)/pi 
        if y > 0:
            theta = 2 - theta
        theta += d_theta

        # Redefine x and y at center of the rect's left side.

        y = sin((2 - theta) * pi) * radius
        x = cos((2 - theta) * pi) * radius

        # Calc the composite rotation angle.
        rotation = (rotation + theta) % 2

        return (Rectangle(x=x,
                          y=y + mods[1]/2,
                          width=hyp,
                          height=mods[1]),
                rotation)


class Modifier(object):
    """ An expression in a macro primitive.

    We interpret gerber expressions according to the following
    grammar, rooted at TOP:

      TOP -> number ESUB
      TOP -> minus number ESUB
      TOP -> variable TOPE
      TOPE ->
      TOPE -> equate E
      TOPE -> op E
      E -> variable ESUB
      E -> number ESUB
      E -> minus number ESUB
      ESUB ->
      ESUB -> op E

    All numerical operations are given the same precedence and are
    grouped right to left.
    """

    scanner = re.Scanner([
            (r'\d*\.\d+', lambda s, tok: Token('number', float(tok))),
            (r'\d+', lambda s, tok: Token('number', float(tok))),
            (r'\$\d+', lambda s, tok: Token('variable', int(tok[1:]))),
            (r'[\+xX/-]', lambda s, tok: Token('op', tok.lower())),
            (r'=', lambda s, tok: Token('equate', None)),
            ])

    def __init__(self, expr):
        self.tokens, remainder = self.scanner.scan(expr)
        if remainder:
            raise InvalidExpression(expr)


    def evaluate(self, values):
        """ Evaluate the expression given a values dict and return the
        resulting value. The values dict may be modified in the
        process (by an equate expression). """
        stack = []
        self._evaluate_top(list(self.tokens), stack, values)
        return self._pop_value(stack, values)


    def _evaluate_top(self, tokens, stack, values):
        """ Evaluate the TOP production. """
        token = tokens.pop(0)

        if token.type == 'number':
            stack.append(token)
            self._evaluate_sube(tokens, stack, values)
        elif token.type == 'variable':
            stack.append(token)
            self._evaluate_tope(tokens, stack, values)
        elif token == ('op', '-'):
            if not tokens:
                raise InvalidExpression(token, stack, values)
            num = tokens.pop(0)
            if num.type != 'number':
                raise InvalidExpression(token, num, stack, values)
            stack.append(Token('number', -num.value))
        else:
            raise InvalidExpression(token, tokens)


    def _evaluate_tope(self, tokens, stack, values):
        """ Evaluate the TOPE production. """
        if not tokens:
            return

        token = tokens.pop(0)

        if token.type == 'equate':
            var = stack.pop()
            self._evaluate_e(tokens, stack, values)
            values[var.value] = self._pop_value(stack, values)
            stack.append(Token('number', values[var.value]))
        elif token.type == 'op':
            self._evaluate_e(tokens, stack, values)
            self._evaluate_op(token, stack, values)
        else:
            raise InvalidExpression(token, tokens)


    def _evaluate_e(self, tokens, stack, values):
        """ Evaluate the E production. """
        token = tokens.pop(0)

        if token.type in ('number', 'variable'):
            stack.append(token)
            self._evaluate_sube(tokens, stack, values)
        elif token == ('op', '-'):
            if not tokens:
                raise InvalidExpression(token, stack, values)
            num = tokens.pop(0)
            if num.type != 'number':
                raise InvalidExpression(token, num, stack, values)
            stack.append(Token('number', -num.value))
        else:
            raise InvalidExpression(token, tokens)


    def _evaluate_sube(self, tokens, stack, values):
        """ Evaluate the SUBE production. """
        if not tokens:
            return

        token = tokens.pop(0)

        if token.type == 'op':
            self._evaluate_e(tokens, stack, values)
            self._evaluate_op(token, stack, values)
        else:
            raise InvalidExpression(token, tokens)


    def _evaluate_op(self, operand, stack, values):
        """ Evaluate the given operand and push the value onto the
        stack. """
        val2 = self._pop_value(stack, values)
        val1 = self._pop_value(stack, values)

        if operand.value == '+':
            val = val1 + val2
        elif operand.value == '-':
            val = val1 - val2
        elif operand.value == 'x':
            val = val1 * val2
        elif operand.value == '/':
            val = val1 / val2
        else:
            raise InvalidExpression(operand, stack)

        stack.append(Token('number', val))


    def _pop_value(self, stack, values):
        """ Pop a value from the stack and return it,
        replacing variables with their values. """

        token = stack.pop()

        if token.type == 'number':
            return token.value
        elif token.type == 'variable':
            return values[token.value]
        else:
            raise InvalidExpression(token, stack, values)


class TraceBuffer(object):
    """
    Map precision-rounded points to traces for fast trace lookup.
    """

    precision = 6

    def __init__(self):
        self.pt2trace = {} # (width, (x, y)) -> Trace

    def add_segment(self, segment, trace):
        """ Add a segment to a trace in the cache. """
        for point in self.get_ends(segment):
            self.pt2trace[trace.width, point] = trace

    def get_trace(self, width, segment):
        """ Given a width and a trace segment, return the Trace
        where that segment should go, or None if no Trace matches. """
        for point in self.get_ends(segment):
            trace = self.pt2trace.get((width, point))
            if trace is not None:
                return trace

    def get_ends(self, segment):
        """ Return the endpoints of the segment as a tuple
        of precision-rounded (x,y) tuples. """
        if isinstance(segment, Arc):
            ends = segment.ends()
        else:
            ends = (segment.p1, segment.p2)

        return ((round(point.x, self.precision),
                 round(point.y, self.precision)) for point in ends)
