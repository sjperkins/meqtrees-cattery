# -*- coding: utf-8 -*-
#
#% $Id$ 
#
#
# Copyright (C) 2002-2007
# The MeqTree Foundation & 
# ASTRON (Netherlands Foundation for Research in Astronomy)
# P.O.Box 2, 7990 AA Dwingeloo, The Netherlands
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <http://www.gnu.org/licenses/>,
# or write to the Free Software Foundation, Inc., 
# 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
#

"""<P>This module implements voltage beam patterns that are read in from FITS files and interpolated
via a PyNode. This implementation allows for a lot of flexibility in how the beam  is
interpolated.</P>

<P align="right">Author: O. Smirnov &lt;<tt>smirnov@astron.nl</tt>&gt;</P>""";

from Timba.TDL import *
from Timba import pynode
from Timba.Meq import meq
from Timba import mequtils

import Meow
from Meow import Context
from Meow import StdTrees

import os
import os.path
import math
import numpy
import random
import Kittens.utils
_verbosity = Kittens.utils.verbosity(name="vb");
dprint = _verbosity.dprint;
dprintf = _verbosity.dprintf;


DEG = math.pi/180;

import EMSSVoltageBeam
from InterpolatedVoltageBeam import unite_shapes

SYM_SEPARATE = None;
SYM_X = "X";
SYM_Y = "Y";


TDLCompileOption("filename_pattern","Filename pattern",["beam_$(hv).pat"],more=str,doc="""<P>
  Pattern for beam filenames. Each beam file contains two elements of the E-Jones matrix. A number of variables will be 
  substituted in the filename pattern, these may be introduced as
  "$var" or "$(var)". Use "$$" for a "$" character. The following variables are recognized:</P>
  <UL>
  <LI><B>$xy</B> or <B>$XY</B>: x or y, or X or Y, for the first or second row of the E-Jones matrix</LI>
  <LI><B>$hv</B> or <B>$HV</B>: v or h, or V or H, for the first or second row of the E-Jones matrix</LI>
  <LI><B>$label</B>: pattern label. Multiple-resolution patterns may be specified, they will all be added together.</LI>
  <LI><B>$freq</B>: frequency label. Multiple per-frequency patterns may be specified, they will be interpolated in.</LI>
  <UL>""");
TDLCompileOption("pattern_labels","Beam pattern labels",[None],more=str);
TDLCompileOption("freq_labels","Beam frequency labels",[None],more=str);
TDLCompileOption("beam_symmetry","Use 90-degree symmetry",{ 
  None:"no, load separate X/Y patterns",
  SYM_X:"yes, XX XY (v) given",
  SYM_Y:"yes, YX YY (h) given"},doc=
  """<P>If the beam has 90-degree rotational symmetry, then its XX/XY and YX/YY components can be derived
  from one another via a 90 degree rotation. If this is the case, then you may use just one set of pattern files
  (either one, as specified by this setting), and let the interpolator apply rotation to derive the other set.
  The beam pattern filename above should't contain a substitutable $(hv) or $(xy) element then.  
  </P>
  """);
TDLCompileOption("spline_order","Spline order for interpolation",[1,2,3,4,5],default=3);
TDLCompileOption("hier_interpol","Use hierarchical interpolation (lm, then frequency)",True);
TDLCompileOption("l_beam_offset","Offset beam pattern in L (deg)",[0.0], more=float,
doc="""<P>By default,the beam reference position (phi=theta=0) is placed at l=m=0 on the sky, i.e. 
at the phase centre. You can use this option to offset the beam pattern.</P>"""),
TDLCompileOption("m_beam_offset","Offset beam pattern in M (deg)",[0.0], more=float,
doc="""<P>By default,the beam reference position (phi=theta=0) is placed at l=m=0 on the sky, i.e. 
at the phase centre. You can use this option to offset the beam pattern.</P>"""),
TDLCompileOption("sky_rotation","Include sky rotation",True,doc="""<P>
  If True, then the beam will rotate on the sky with parallactic angle. Use for alt-az mounts.
  </P>""");
TDLCompileOption("randomize_rotation","Include random rotational offsets",False,doc="""<P>
  If True, then each station's beam will be rotated by an arbitrary amount. This can help randomize the sidelobes.
  </P>""");
TDLCompileOption("verbose_level","Debugging message level",[None,1,2,3],more=int);

def _cells_grid (obj,axis):
  """helper function to get a grid out of the cells object. Returns None if none is found"""
  if hasattr(obj,'cells') and hasattr(obj.cells.grid,axis):
    return obj.cells.grid[axis];
  else:
    return None;


class EMSSPolarBeamInterpolatorNode (pynode.PyNode):
  def __init__ (self,*args):
    pynode.PyNode.__init__(self,*args);
    # Maintain a global dict of VoltageBeam objects per each filename set, so that we reuse them
    # We also reset this dict each time a node is created (otherwise the objects end up being reused
    # even after the tree has been rebuilt.)
    global _voltage_beams;
    _voltage_beams = {};
    self._vbs = None;

  def update_state (self,mystate):
    """Standard function to update our state""";
    mystate('filename',[]);
    mystate('spline_order',3);
    mystate('hier_interpol',True);
    mystate('l_0',0.0);
    mystate('m_0',0.0);
    mystate('verbose',0);
    mystate('missing_is_null',False);
    mystate('beam_symmetry',None);
    # other init
    mequtils.add_axis('l');
    mequtils.add_axis('m');
    self._freqaxis = mequtils.get_axis_number("freq");
    _verbosity.set_verbose(self.verbose);

  def init_voltage_beams (self):
    """initializes VoltageBeams supplied to this node"""
    # maintain a global dict of VoltageBeam objects per each filename set, so that we reuse them
    if self._vbs is not None:
      return self._vbs;
    # we get VoltageBeam objects from global dict, or init new ones if not already defined
    global _voltage_beams;
    self._vbs = [];
    # setup rotation and y-arg combinations according to symmetry modes
    # only V/X beam supplied. H/Y must be rotated by 90 degrees and columns swapped.
    if self.beam_symmetry == SYM_X:
      rotations = 0,90;
      yargflips = False,True; 
    # only H/Y beam supplied. V/X rotated/swapped
    elif self.beam_symmetry == SYM_Y:
      rotations = 90,0;
      yargflips = True,False;
    # else both beams supplied
    else:
      rotations = 0,0;
      yargflips = False,False;
    # loop over files
    for per_label in self.filename:
      vbmat = [];
      for xy,rotate,yargflip,files_per_xy in zip(('x','y'),rotations,yargflips,per_label):
        for xy1,yarg0 in zip(('x','y'),(False,True)):
          yarg = yarg0^yargflip;
          vbkey = tuple(files_per_xy),yarg,rotate;
          vb = _voltage_beams.get(vbkey)
          if vb is None:
            dprint(1,"Loading beams for %s%s from"%(xy,xy1),files_per_xy);
            vb = _voltage_beams[vbkey] = EMSSVoltageBeam.EMSSVoltageBeamPS(files_per_xy,y=yarg,
                        spline_order=self.spline_order,hier_interpol=self.hier_interpol,rotate=rotate,
                        verbose=self.verbose);
          vbmat.append(vb);
      self._vbs.append(vbmat);
    return self._vbs;

  def interpolate_per_source (self,lm,nlm,nsrc,dl,dm,grid,vbs):
    # Loops over all source coordinates in the lm tensor, interpolates beams for them, and returns the resulting vellsets.
    # This is the "fail-safe" method, as it interpolates per-source, and therefore allows for the source lm arrays to have different
    # time/frequency dependencies per source.
    vellsets = [];
    for isrc in range(nsrc):
      l,m = lm.vellsets[isrc*nlm].value,lm.vellsets[isrc*nlm+1].value;
      # apply pointing offsets, if any
      if dl is not None:
        # unite shapes just in case, since l/m and dl/dm may have time/freq axes
        l,dl = unite_shapes(l,dl);
        m,dm = unite_shapes(m,dm);
        l,m = l-dl,m-dm;
      grid['l'],grid['m'] = l,m;
      # loop over all 2x2 matrices (we may have several, they all need to be added)
      E = [None]*4;
      for vbmat in vbs:
        for i,vb in enumerate(vbmat):
          beam = vb.interpolate(freqaxis=self._freqaxis,**grid);
          if E[i] is None:
            E[i] = meq.complex_vells(beam.shape);
          E[i][...] += beam[...];
      # make vellsets
      for ej in E:
        vellsets.append(meq.vellset(ej));
    return vellsets;

  def interpolate_batch (self,lm,nlm,nsrc,dl,dm,grid,vbs):
    # A faster version of interpolate_per_source(), which assumes that all lm's have the same shape, and stacks them
    # into a single array for a single interpolation call.
    # If there's a shape mismatch, it'll fall back to interpolate_per_soucre
    l0,m0 = lm.vellsets[0].value,lm.vellsets[1].value;
    for isrc in range(nsrc):
      l,m = lm.vellsets[isrc*nlm].value,lm.vellsets[isrc*nlm+1].value;
      # apply pointing offsets, if any
      if dl is not None:
        # unite shapes just in case, since l/m and dl/dm may have time/freq axes
        dl,dm = unite_shapes(dl,dm);
        l,dl = unite_shapes(l,dl);
        m,dm = unite_shapes(m,dm);
        l,m = l-dl,m-dm;
      else:
        l,m = unite_shapes(l,m);
      if not isrc:
        lm_shape = l.shape;
        cubeshape = [nsrc]+list(lm_shape);
        lcube = numpy.zeros(cubeshape,float);
        mcube = numpy.zeros(cubeshape,float);
      else:
        if l.shape != lm_shape:
          dprint(1,"l/m shapes unequal at source %d, falling back to per-source interpolation"%isrc);
          return self.interpolate_per_source(lm,nlm,nsrc,dl,dm,grid,vbs);
      lcube[isrc,...] = l;
      mcube[isrc,...] = m;
    # ok, we've stacked things into lm cubes, interpolate
    grid['l'],grid['m'] = lcube.ravel(),mcube.ravel();
    # loop over all 2x2 matrices (we may have several, they all need to be added)
    E = [None]*4;
    for vbmat in vbs:
      for i,vb in enumerate(vbmat):
        beam = vb.interpolate(freqaxis=self._freqaxis,**grid);
        if E[i] is None:
          E[i] = beam;
        else:
          E[i] += beam;
    # The l/m cubes have a shape of [nsrcs,lm_shape].
    # These are raveled for interpolation, so the resulting Es have a shape of [nsrcs*num_lm_points,num_freq]
    # Reshape them properly. Note that there's an extra "soucre" axis at the front, so the frequency axis
    # is off by 1.
    if len(cubeshape) <= self._freqaxis+1:
      cubeshape += [1]*(self._freqaxis - len(cubeshape) + 2);
    cubeshape[self._freqaxis+1] = len(grid['freq']);
    E = [ ej.reshape(cubeshape) for ej in E ];
    # now tease the E's apart plane by plane
    # make vellsets
    vellsets = [];
    for isrc in range(nsrc):
      for ej in E:
        ejplane = ej[isrc,...];
        value = meq.complex_vells(ejplane.shape,ejplane);
        vellsets.append(meq.vellset(value));
    return vellsets;


  def get_result (self,request,*children):
    # get list of VoltageBeams
    vbs = self.init_voltage_beams();
    # now, figure out the lm and time/freq grid
    # lm may be a 2-vector or an Nx2 tensor
    lm = children[0];
    dims = getattr(lm,'dims',[len(lm.vellsets)]);
    if len(dims) == 2 and dims[1] in (2,3): 
      nsrc,nlm = dims;
      tensor = True;
    elif len(dims) == 1 and dims[0] in (2,3):
      nsrc,nlm = 1,dims[0];
      tensor = False;
    else:
      raise TypeError,"expecting a 2/3-vector or an Nx2/3 matrix for child 0 (lm)";
    # pointing offsets (child 1) are optional
    if len(children) > 1:
      dlm = children[1]; 
      if len(dlm.vellsets) != 2:
        raise TypeError,"expecting a 2-vector for child 1 (dlm)";
      dl,dm = dlm.vellsets[0].value,dlm.vellsets[1].value;
    else:
      dl = dm = None;
    # setup grid dict that will be passed to VoltageBeam.interpolate
    grid = dict();
    for axis in 'time','freq':
      values = _cells_grid(lm,axis);
      if values is None:
        values = _cells_grid(request,axis);
      if values is not None:
        grid[axis] = values;
    # accumulate per-source EJones tensor
    vellsets = self.interpolate_batch(lm,nlm,nsrc,dl,dm,grid,vbs);
    # create result object
    cells = request.cells if vbs[0][0].hasFrequencyAxis() else getattr(lm,'cells',None);
    result = meq.result(vellsets[0],cells=cells);
    result.vellsets[1:] = vellsets[1:];
    result.dims = (nsrc,2,2) if tensor else (2,2);
    return result;


from Siamese.OMS import Utils
XY2HV = dict(x='v',y='h');

def make_beam_filename (filename_pattern,xy,label,freq):
  """Makes beam filename for the given correlation and real/imaginary component (one of "re" or "im")"""
  hv = XY2HV[xy.lower()];
  return Utils.substitute_pattern(filename_pattern,
    xy=xy.lower(),XY=xy.upper(),
    hv=hv,HV=hv.upper(),
    label=label,freq=freq);

def make_beam_node (beam,pattern,*children):
  """Makes beam interpolator node for the given filename pattern.""";
  filenames = [];
  labels = re.split('[,\s]',pattern_labels) if pattern_labels else [""];
  freqs = re.split('[,\s]',freq_labels) if freq_labels else [""];
  for label in labels:
    per_label = [];
    for xy in "xy":        
      per_xy = [];
      for freq in freqs:
        filename = make_beam_filename(pattern,xy,label,freq);
        if not os.path.exists(filename):
          raise RuntimeError,"Can't find beam pattern file %s"%filename;
        per_xy.append(filename);
      per_label.append(per_xy);
    filenames.append(per_label); 
  # now make interpolator node
  import InterpolatedBeams
  if children[-1] is None:
    children = children[:-1];
  beam << Meq.PyNode(class_name="EMSSPolarBeamInterpolatorNode",module_name=__file__,
                     filename=filenames,
                     missing_is_null=False,spline_order=spline_order,verbose=verbose_level or 0,
                     l_beam_offset=l_beam_offset*DEG,m_beam_offset=m_beam_offset*DEG, 
                     beam_symmetry=beam_symmetry,
                     children=children);




def compute_jones (Jones,sources,stations=None,pointing_offsets=None,inspectors=[],label='E',**kw):
  stations = stations or Context.array.stations;
  ns = Jones.Subscope();

  # declare an inspector node for the Jones matrix -- will be defined below
  insp = Jones.scope.inspector(label);
  inspectors += [ insp ];

  # loop over sources
  for src in sources:
    # If sky rotation and/or pointing offsets are in effect, we have a per-station beam.
    # Otherwise the beam is the same for all stations.
    if sky_rotation or pointing_offsets:
      for p in stations:
        lm = src.direction.lm();
        # apply rotation to put sources into the antenna frame
        if sky_rotation:
          xyz = Context.array.xyz(p);
          pa_rot = Context.observation.phase_centre.pa_rot(xyz);
          lm = ns.lmrot(src,p) <<  Meq.MatrixMultiply(pa_rot,src.direction.lm());
        # apply offset in the node (so pointing offsets are interpreted in the azel frame, if rotating)
        make_beam_node(Jones(src,p),filename_pattern,lm,pointing_offsets and pointing_offsets(p));
    else:
      make_beam_node(Jones(src),filename_pattern,src.direction.lm());
      for p in stations:
        Jones(src,p) << Meq.Identity(Jones(src));

  # define an inspector
  if sky_rotation or pointing_offsets:
    # Jones inspector is per-source, per-station
    insp << StdTrees.define_inspector(Jones,sources,stations,label=label);
  else:
    # Jones inspector is per-source
    insp << StdTrees.define_inspector(Jones,sources,label=label);

  return Jones;


def compute_jones_tensor (Jones,sources,stations,lmn=None,pointing_offsets=None,inspectors=[],label='E',**kw):
  stations = stations or Context.array.stations;
  ns = Jones.Subscope();
  
  # if sky rotation is in effect, ignore the lmn tensor
  if sky_rotation or randomize_rotation:
    lmn = None;

  # see if sources have a "beam_lm" or "_lm_ncp" attribute
  lmsrc = [ src.get_attr("beam_lm",None) or src.get_attr("_lm_ncp",None) for src in sources ];
  
  # if all source have the attribute, create lmn tensor node (and override the lmn argument)
  if all([lm is not None for lm in lmsrc]):
    lmn = ns.lmT << Meq.Constant(lmsrc);
  
  if not lmn:
    lmn = ns.lmT << Meq.Composer(dims=[0],*[ src.direction.lm() for src in sources ]);

  # create station tensor
  # If sky rotation and/or pointing offsets are in effect, we have a per-station beam.
  # Otherwise the beam is the same for all stations.
  if sky_rotation or pointing_offsets or randomize_rotation:
    for p in stations:
      angle = None
      if randomize_rotation:
        angle = ns.rot0(p) << random.uniform(-math.pi,math.pi);
      # apply rotation to put sources into the antenna frame
      # lmn is an Nx2 tensor. Multiply that by the _inverse_ (i.e. transpose) of the rotation matrix on the _right_
      # to get the equivalent rotation.
      if sky_rotation:
        xyz = Context.array.xyz(p);
        pa = Context.observation.phase_centre.pa(xyz);
        angle = (ns.rot1(p) << angle + pa) if angle is not None else pa;
      # if a rotation angle is set, make the matrix
      if angle is not None:
        rcos = ns.rot_cos(p) << Meq.Cos(angle);
        rsin = ns.rot_sin(p) << Meq.Sin(angle);
        ns.Rot(p) << Meq.Matrix22(rcos,rsin,-rsin,rcos);  # apply '-' because we want the inverse of the PA
        lm = ns.lmTrot(p) <<  Meq.MatrixMultiply(lmn,ns.Rot(p));
      else:
        lm = lmn;
      # apply offset in the node (so pointing offsets are interpreted in the azel frame, if rotating)
      if pointing_offsets:
        make_beam_node(Jones(p),filename_pattern,lm,pointing_offsets(p));
      else:
        make_beam_node(Jones(p),filename_pattern,lm);
    return Jones;
  else:
    make_beam_node(Jones,filename_pattern,lmn);
    return lambda p,J=Jones:J;
