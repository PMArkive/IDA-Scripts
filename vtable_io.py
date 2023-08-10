import idc
import idautils
import idaapi
import json
import ctypes
import time
import re

from sys import version_info
from dataclasses import dataclass

# Let's go https://www.blackhat.com/presentations/bh-dc-07/Sabanal_Yason/Paper/bh-dc-07-Sabanal_Yason-WP.pdf

class RTTICompleteObjectLocator(ctypes.Structure):
	_fields_ = [
		("signature",  ctypes.c_uint32), 					# signature
		("offset",  ctypes.c_uint32), 						# offset of this vtable in complete class (from top)
		("cdOffset",  ctypes.c_uint32), 					# offset of constructor displacement
		("pTypeDescriptor",  ctypes.c_uint32), 				# ref TypeDescriptor
		("pClassHierarchyDescriptor",  ctypes.c_uint32), 	# ref RTTIClassHierarchyDescriptor
	]


class TypeDescriptor(ctypes.Structure):
	_fields_ = [
		("pVFTable", ctypes.c_uint32), 						# reference to RTTI's vftable
		("spare", ctypes.c_uint32), 						# internal runtime reference
		("name", ctypes.c_uint8), 							# type descriptor name (no varstruct needed since we don't use this)
	]


class RTTIClassHierarchyDescriptor(ctypes.Structure):
	_fields_ = [
		("signature", ctypes.c_uint32), 					# signature
		("attribs", ctypes.c_uint32), 						# attributes
		("numBaseClasses", ctypes.c_uint32), 				# # of items in the array of base classes
		("pBaseClassArray", ctypes.c_uint32), 				# ref BaseClassArray
	]


class RTTIBaseClassDescriptor(ctypes.Structure):
	_fields_ = [
		("pTypeDescriptor", ctypes.c_uint32),				# ref TypeDescriptor
		("numContainedBases", ctypes.c_uint32),				# # of sub elements within base class array
		("mdisp", ctypes.c_uint32),  						# member displacement
		("pdisp", ctypes.c_uint32),							# vftable displacement
		("vdisp", ctypes.c_uint32), 						# displacement within vftable
		("attributes", ctypes.c_uint32), 					# base class attributes
		("pClassDescriptor", ctypes.c_uint32), 				# ref RTTIClassHierarchyDescriptor
	]


class base_class_type_info(ctypes.Structure):
	_fields_ = [
		("basetype", ctypes.c_uint32), 						# Base class type
		("offsetflags", ctypes.c_uint32), 					# Offset and info
	]


class class_type_info(ctypes.Structure):
	_fields_ = [
		("pVFTable", ctypes.c_uint32), 						# reference to RTTI's vftable (__class_type_info)
		("pName", ctypes.c_uint32), 						# ref to type name
	]

# I don't think this is right, but every case I found looked to be correct
# This might be a vtable? IDA sometimes says it is but not always
# Plus sometimes the flags member is 0x1, so it's not a thisoffs. Weird
class pointer_type_info(class_type_info):
	_fields_ = [
		("flags", ctypes.c_uint32),							# Flags or something else
		("pType", ctypes.c_uint32),							# ref to type
	]

class si_class_type_info(class_type_info):
	_fields_ = [
		("pParent", ctypes.c_uint32), 						# ref to parent type
	]

class vmi_class_type_info(class_type_info):
	_fields_ = [
		("flags", ctypes.c_uint32), 						# flags
		("basecount", ctypes.c_uint32), 					# # of base classes
		("pBaseArray", base_class_type_info), 				# array of BaseClassArray
	]

def create_vmi_class_type_info(ea):
	bytestr = idaapi.get_bytes(ea, ctypes.sizeof(vmi_class_type_info))
	tinfo = vmi_class_type_info.from_buffer_copy(bytestr)

	# Since this is a varstruct, we create a dynamic class with the proper size and type and return it instead
	class vmi_class_type_info_dynamic(class_type_info):
		_fields_ = [
			("flags", ctypes.c_uint32),
			("basecount", ctypes.c_uint32),
			("pBaseArray", base_class_type_info * tinfo.basecount),
		]
	
	return vmi_class_type_info_dynamic


# Steps to retrieve vtables on Windows (MSVC):
#	1. Get RTTI's vftable (??_7type_info@@6B@)
#	2. Iterate over xrefs to, which are all TypeDescriptor objects
# 		a. Of course don't load up the function that uses it
# 	3. At each xref load up xrefs to again
# 		a. There should only be at least 2, the important ones are RTTICompleteObjectLocator's AKA COL (there can be more than 1)
# 		b. To discern which one is which, just see if there's a label at the address
# 			- If there is, then that one is RTTIClassHierarchyDescriptor, so skip it
# 	4. The current ea position at each xref should be at RTTICompleteObjectLocator::pTypeDescriptor, so subtract 12 to get to the beginning of the struct
# 	5. Find xrefs to each. There should only be one, and it should be its vtable
# 		a. Each COL has an offset which will shows where its vtable starts, so running too far over the table will be easier to detect
#
# Steps to retrieve vtables on Linux (GCC and maybe Clang)
#	1. Get RTTI's vftable (_ZTVN10__cxxabiv117__class_type_infoE, 
# 		_ZTVN10__cxxabiv120__si_class_type_infoE, and _ZTVN10__cxxabiv121__vmi_class_type_infoE)
# 	2. First, before doing anything, shove each xref of type_info object into some sort of structure
# 		a. There's no easy way to cheese discerning which xref is the actual vtable, unless we want to start parsing IDA comments
# 	3. Once each type_info object and their references are loaded, get the xrefs from each pVFTable
# 	4. There will probably be more than one xref.
# 		a. To discern which one is a vtable, if the xref lies in another type_info object, then it's not a vtable
# 		b. The remaining xref(s) is indeed a vtable

# Class for windows type info, helps organize things
@dataclass(frozen=True)
class WinTI(object):
	typedesc: int
	name: str
	cols: list[int]
	vtables: list[int]

# Class for function lists (what is held in the json)
@dataclass(frozen=True)
class FuncList:
	thisoffs: int
	funcs: list#[VFunc]

# Idiot proof IDA wait box
class WaitBox(object):
	def __init__(self):
		self.buffertime = 0.0
		self.shown = False
		self.msg = ""

	def _show(self, msg):
		self.msg = msg
		if self.shown:
			idaapi.replace_wait_box(msg)
		else:
			idaapi.show_wait_box(msg)
			self.shown = True

	def show(self, msg, buffertime = 0.1):
		if msg == self.msg:
			return

		if buffertime > 0.0:
			if time.time() - self.buffertime < buffertime:
				return
			self.buffertime = time.time()
		self._show(msg)

	def hide(self):
		if self.shown:
			idaapi.hide_wait_box()
			self.shown = False

	# Dtor doesn't work? Lol idk
	def __del__(self):
		self.hide()

# Virtual class tree
class VClass(object):
	def __init__(self, *args, **kwargs):
		self.name = kwargs.get("name", "")
		# dict[classname, VClass]
		self.baseclasses = kwargs.get("baseclasses", {})
		# Same as Linux json, dict[thisoffs, funcs]
		self.vfuncs = kwargs.get("vfuncs", {})
		# Written to when writing to Windows, dict[thisoffs, [VFunc]]
		self.vfuncnames = kwargs.get("vfuncnames", {})
		# Exists solely to speed up checking for inherited functions
		self.postnames = set()

	def __str__(self):
		return f"{self.name} (baseclasses = {self.baseclasses}, vfuncs = {self.vfuncs})"

	def parse(self, colea, wintable):
		col = get_class_from_ea(RTTICompleteObjectLocator, colea)
		thisoffs = col.offset

		# Already parsed
		if self.name in wintable.keys():
			if thisoffs in wintable[self.name].vfuncs.keys():
				return

		xrefs = list(idautils.XrefsTo(colea))
		if len(xrefs) != 1:
			print(f"[VTABLE IO] Multiple vtables point to same COL - {self.name} at {colea:#x}")

		vtable = xrefs[0].frm + ctypes.sizeof(ea_t)
		self.vfuncs[thisoffs] = parse_vtable_addresses(vtable)

# TODO; This is created for each function in the json and for each function in each vtable
# This clearly does this for multiple of each function, so there needs to be a way to
# cache each function and reuse it for each vtable
# Possible pain point is differentiating between inheritedness
@dataclass
class VFunc:
	ea: int
	mangledname: str
	inherited: bool
	name: str
	postname: str
	sname: str

def make_vfunc(ea=idc.BADADDR, mangledname="", inherited=False):
	name = ""
	postname = ""
	sname = ""
	if mangledname:
		name = idaapi.demangle_name(mangledname, SHORTDN) or mangledname
		if name:
			postname = get_func_postname(name)
			sname = postname.split("(")[0]
	return VFunc(ea, mangledname, inherited, name, postname, sname)

OS_Linux = 0
OS_Win = 1

FUNCS = 0

WAITBOX = WaitBox()

if idc.__EA64__:
	ea_t = ctypes.c_uint64
	ptr_t = ctypes.c_int64
	get_ptr = idaapi.get_qword
	# Calling this a lot so we'll speed up the invocations by manually implementing this here
	def is_ptr(f): return (f & idaapi.MS_CLS) == idaapi.FF_DATA and (f & idaapi.DT_TYPE) == idaapi.FF_QWORD
else:
	ea_t = ctypes.c_uint32
	ptr_t = ctypes.c_int32
	get_ptr = idaapi.get_dword
	def is_ptr(f): return (f & idaapi.MS_CLS) == idaapi.FF_DATA and (f & idaapi.DT_TYPE) == idaapi.FF_DWORD

def is_off(f): return f & (idaapi.FF_0OFF|idaapi.FF_1OFF) != 0
def is_code(f): return (f & idaapi.MS_CLS) == idaapi.FF_CODE
def has_any_name(f): return (f & idaapi.FF_ANYNAME) != 0
SHORTDN = idc.get_inf_attr(idc.INF_SHORT_DN)

def get_os():
	ftype = idaapi.get_file_type_name()
	if "ELF" in ftype:
		return OS_Linux
	elif "PE" in ftype:
		return OS_Win
	return -1

# Read a ctypes class from an ea
def get_class_from_ea(classtype, ea):
	bytestr = idaapi.get_bytes(ea, ctypes.sizeof(classtype))
	return classtype.from_buffer_copy(bytestr)

# Anything past Classname::
# Thank you CTFPlayer::SOCacheUnsubscribed...
def get_func_postname(name):
	retname = name
	if retname[:retname.find("(")].rfind("::") != -1:
		retname = retname[retname[:retname.find("(")].rfind("::")+2:]

	return retname

def parse_vtable_names(ea):
	funcs = []

	while ea != idc.BADADDR:
		# Using flags sped this up by a lot
		# Went from 4 secs to ~1.3
		flags = idaapi.get_full_flags(ea)
		if not is_off(flags) or not is_ptr(flags):
			break

		if idaapi.has_name(flags):
			break

		offs = get_ptr(ea)
		fflags = idaapi.get_full_flags(offs)
		if not idaapi.is_func(fflags):
			break

		name = idaapi.get_name(offs)
		funcs.append(name)

		ea = idaapi.next_head(ea, idc.BADADDR)
	return funcs

def parse_vtable_addresses(ea):
	funcs = []

	while ea != idc.BADADDR:
		flags = idaapi.get_full_flags(ea)
		if not is_off(flags) or not is_ptr(flags):
			break

		offs = get_ptr(ea)
		fflags = idaapi.get_full_flags(offs)
		if not has_any_name(fflags):
			break

#		if not idaapi.is_func(fflags):# or not idaapi.has_name(fflags):
		# Sometimes IDA doesn't think a function is a function
		# This is all CSteamWorksGameStatsUploader's fault :(
		if not is_code(fflags):
			break

		funcs.append(make_vfunc(ea=offs))

		ea = idaapi.next_head(ea, idc.BADADDR)
	return funcs

def parse_si_tinfo(ea, tinfos):
	for xref in idautils.XrefsTo(ea):
		tinfo = get_class_from_ea(si_class_type_info, xref.frm)
		tinfos[xref.frm + si_class_type_info.pParent.offset] = tinfo.pParent

def parse_pointer_tinfo(ea, tinfos):
	for xref in idautils.XrefsTo(ea):
		tinfo = get_class_from_ea(pointer_type_info, xref.frm)
		tinfos[xref.frm + pointer_type_info.pType.offset] = tinfo.pType

def parse_vmi_tinfo(ea, tinfos):
	for xref in idautils.XrefsTo(ea):
		tinfotype = create_vmi_class_type_info(xref.frm)
		tinfo = get_class_from_ea(tinfotype, xref.frm)

		for i in range(tinfo.basecount):
			offset = vmi_class_type_info.pBaseArray.offset + i * ctypes.sizeof(base_class_type_info)
			basetinfo = get_class_from_ea(base_class_type_info, xref.frm + offset)
			tinfos[xref.frm + offset + base_class_type_info.basetype.offset] = basetinfo.basetype

def get_tinfo_vtables(ea, tinfos, vtables):
	if ea == idc.BADADDR:
		return

	for tinfoxref in idautils.XrefsTo(ea, idaapi.XREF_DATA):
		count = 0
		mangled = idaapi.get_name(tinfoxref.frm)
		demangled = idc.demangle_name(mangled, SHORTDN)
		if demangled is None:
			print(f"[VTABLE IO] Invalid name at {tinfoxref.frm:#x}")
			continue

		classname = demangled[len("`typeinfo for'"):]
		for xref in idautils.XrefsTo(tinfoxref.frm, idaapi.XREF_DATA):
			if xref.frm not in tinfos.keys():
				# If address lies in a function
				if idaapi.is_func(idaapi.get_full_flags(xref.frm)):
					continue

				count += 1
				vtables[classname] = vtables.get(classname, []) + [xref.frm]

def read_vtables_linux():
	f = idaapi.ask_file(1, "*.json", "Select a file to export to")
	if not f:
		return
		
	WAITBOX.show("Parsing typeinfo")

	# Step 1 and 2, crawl xrefs and stick the inherited class type infos into a structure
	# After this, we can run over the xrefs again and see which xrefs come from another structure
	# The remaining xrefs are either vtables or weird math in a function
	xreftinfos = {}

	def getparse(name, fn, quiet=False):
		tinfo = idc.get_name_ea_simple(name)
		if tinfo == idc.BADADDR and not quiet:
			print(f"[VTABLE IO] Type info {name} not found. Skipping...")
			return None

		if fn is not None:
			fn(tinfo, xreftinfos)
		return tinfo

	# Don't need to parse base classes
	tinfo = getparse("_ZTVN10__cxxabiv117__class_type_infoE", None)	
	tinfo_pointer = getparse("_ZTVN10__cxxabiv119__pointer_type_infoE", parse_pointer_tinfo, True)
	tinfo_si = getparse("_ZTVN10__cxxabiv120__si_class_type_infoE", parse_si_tinfo)	
	tinfo_vmi = getparse("_ZTVN10__cxxabiv121__vmi_class_type_infoE", parse_vmi_tinfo)
	
	if len(xreftinfos) == 0:
		print("[VTABLE IO] No type infos found. Are you sure you're in a C++ binary?")
		return

	# Step 3, crawl xrefs to again and if the xref is not in the type info structure, then it's a vtable
	WAITBOX.show("Discovering vtables")
	vtables = {}
	get_tinfo_vtables(tinfo, xreftinfos, vtables)
	get_tinfo_vtables(tinfo_pointer, xreftinfos, vtables)
	get_tinfo_vtables(tinfo_si, xreftinfos, vtables)
	get_tinfo_vtables(tinfo_vmi, xreftinfos, vtables)

	# Now, we have a list of vtables and their respective classes
	WAITBOX.show("Parsing vtables")
	jsondata = parse_vtables(vtables)

	WAITBOX.show("Writing to file")
	with open(f, "w") as f:
		json.dump(jsondata, f, indent=4, sort_keys=True)

def read_ti_win():
	# Step 1, get the vftable of type_info
	type_info = idc.get_name_ea_simple("??_7type_info@@6B@")
	if type_info is None:
		print("[VTABLE IO] type_info not found. Are you sure you're in a C++ binary?")
		return
	
	tis = {}

	# Step 2, get all xrefs to type_info
	# Get type descriptor
	for typedesc in idautils.XrefsTo(type_info):
		ea = typedesc.frm
		if idaapi.get_func(ea) is not None:
			continue

		try:
			classname = idaapi.demangle_name(idc.get_name(ea), SHORTDN)
			classname = classname.removeprefix("class ")
			classname = classname.removesuffix(" `RTTI Type Descriptor'")
		except:
			print(f"[VTABLE IO] Invalid vtable name at {ea:#x}")
			continue

		cols = []
		vtables = []

		# Then figure out which xref is a/the COL
		for xref in idautils.XrefsTo(typedesc.frm):
			ea = xref.frm

			# Dynamic cast
			func = idaapi.get_func(ea)
			if func is not None:
				continue

			name = idaapi.get_name(ea)
			# Class type descriptor and random global data
			# Kind of a hack but let's assume no one will rename these
			if name and (name.startswith("??_R1") or name.startswith("off_")):
				continue

			ea -= 4
			name = idaapi.get_name(ea)
			# Catchable types
			if name and name.startswith("__CT"):
				continue

			# COL
			ea -= 8
			workaround = False
			if idaapi.is_unknown(idaapi.get_full_flags(ea)):
				print(f"[VTABLE IO] Possible COL is unknown at {ea:#x}. This may be an unreferenced vtable. Trying workaround...")
				# This might be a bug with IDA, but sometimes the COL isn't analyzed
				# If there's still a reference, then we can still trace back
				# If there is a list of functions (or even just one), then it's probably a vtable, 
				# but we'll still warn the user that it might be garbage
				refs = list(idautils.XrefsTo(ea))
				if len(refs) == 1:
					vtable = refs[0].frm + 4
					tryfunc = get_ptr(vtable + ctypes.sizeof(ea_t))
					func = idaapi.get_func(tryfunc)
					if func is not None:
						print(f" - Workaround successful. Please assure that {vtable:#x} is a vtable.")
						workaround = True
				
				if not workaround:
					print(" - Workaround failed. Skipping...")
					continue

			name = idaapi.get_name(ea)
			if not workaround and (not name or not name.startswith("??_R4")):
				print(f"[VTABLE IO] Invalid name at {ea:#x}. Possible unwind info. Ignoring...")
				continue

			# Now that we have the COL, we can use it to find the vtable that utilizes it and its thisoffs
			# We need to use this later because of overloads so we cache it in a list
			refs = list(idautils.XrefsTo(ea))
			if len(refs) != 1:
				print(f"[VTABLE IO] Multiple vtables point to same COL - {name} at {ea:#x}")
				continue

			cols.append(ea)
			vtable = refs[0].frm + 4
			vtables.append(vtable)

		# Can have RTTI without a vtable
		tis[classname] = WinTI(typedesc.frm, classname, cols, vtables)
	
	return tis

def parse_vtables(vtables):
	jsondata = {}
	ptrsize = ctypes.sizeof(ea_t)
	for classname, tables in vtables.items():
		# We don't *need* to do any sort of sorting in Linux and can just capture the thisoffset
		# The Windows side of the script can organize later
		for ea in tables:
			thisoffs = get_ptr(ea - ptrsize)

			funcs = parse_vtable_names(ea + ptrsize)
			# Can be zero if there's an xref in the global offset table (.got) section
			# Fortunately the parse_vtable function doesn't grab anything from there
			if funcs:
				classdata = jsondata.get(classname, {})
				classdata[ptr_t(thisoffs).value] = funcs
				jsondata[classname] = classdata

	return jsondata

# See if the thunk is actually a thunk and jumps to
# a function in the vtable
def is_thunk(thunkfunc, targetfuncs):
	ea = thunkfunc.ea
	func = idaapi.get_func(ea)
	funcend = func.end_ea

#	if funcend - ea > 20:	# Highest I've seen is 13 opcodes but this works ig
#		return False

	addr = idc.next_head(ea, funcend)

	if addr == idc.BADADDR:
		return False

	b = idaapi.get_byte(addr)
	if b in (0xEB, 0xE9):
		insn = idaapi.insn_t()
		idaapi.decode_insn(insn, addr)
		jmpaddr = insn.Op1.addr
		return any(jmpaddr == i.ea for i in targetfuncs)

	return False

def build_export_table(linlist, winlist):
	instance = (int, long) if version_info[0] < 3 else int
	for i, v in enumerate(linlist):
		if isinstance(v, instance):
			linlist = linlist[:i]		# Skipping thisoffs
			break

	listnode = linlist[:]

	for i, v in enumerate(linlist):
		name = str(v)
		if name.startswith("__cxa"):
			listnode[i] = None
			continue

		s = "L{:<6}".format(i)
		try:
			s += " W{}".format(winlist.index(name))
		except:
			pass

		funcname = idc.demangle_name(name, SHORTDN)
		s = "{:<16} {}".format(s, funcname)
		listnode[i] = s

	return [i for i in listnode if i != None]

def read_vtables_win(classname, ti, wintable, baseclasses):
	if classname in wintable.keys():
		return

	vclass = wintable.get(classname, VClass(name=classname, baseclasses=baseclasses))
	for colea in ti.cols:
		vclass.parse(colea, wintable)

	wintable[classname] = vclass

def read_tinfo_win(classname, ti, winti, wintable, baseclasses):
	# Strange cases where there is a base class descriptor with no vtable
	if classname not in winti.keys():
		return

	if classname in wintable.keys():
		return
	
	# No COLs, but we still keep the type in the wintable
	if not ti.cols:
		wintable[classname] = VClass(name=classname, baseclasses=baseclasses)
		return

	# So essentially we just run through each base class in the hierarchy descriptor 
	# and recursively parse the base classes of the base classes
	# Sort of like a reverse insertion sort only not really a sort
	for colea in ti.cols:
		col = get_class_from_ea(RTTICompleteObjectLocator, colea)
		hierarchydesc = get_class_from_ea(RTTIClassHierarchyDescriptor, col.pClassHierarchyDescriptor)
		numitems = hierarchydesc.numBaseClasses
		arraystart = hierarchydesc.pBaseClassArray

		# Go backwards because we should start parsing from the basest base class
		for i in range(numitems - 1, -1, -1):
			offset = arraystart + i * ctypes.sizeof(ptr_t)
			descea = get_ptr(offset)
			parentname = idaapi.demangle_name(idaapi.get_name(descea), SHORTDN)
			if not parentname:
				# Another undefining IDA moment
#				print(f"[VTABLE IO] Invalid parent name at {offset:#x}")
				typedesc = get_ptr(descea)
				parentname = idaapi.demangle_name(idaapi.get_name(typedesc), SHORTDN)

				# Should be impossible since this is the type descriptor
				if not parentname:
					print(f"[VTABLE IO] Invalid parent name at {offset:#x} - type descriptor at {typedesc:#x}")
					continue

				parentname = parentname.removeprefix("class ")
				parentname = parentname.removesuffix(" `RTTI Type Descriptor'")
			else:
				parentname = parentname[:parentname.find("::`RTTI Base Class Descriptor")]

			# End of the line
			if i == 0:
				read_vtables_win(classname, winti[parentname], wintable, baseclasses)
			elif parentname in winti.keys():
				read_tinfo_win(parentname, winti[parentname], winti, wintable, baseclasses)
				# Once again relying on dicts being ordered
				baseclasses[parentname] = wintable[parentname]

def gen_win_tables(winti):
	# So first we start looping windows typeinfos because
	# we're going to go from the COL -> ClassHierarchyDescriptor -> BaseClassArray
	# The reason why we're doing this is because of subclass overloads
	# For a history lesson, see https://github.com/Scags/IDA-Scripts/blob/125f1877a24da48062e62efcfb7d8a63e3bd939b/vtable_io.py#L251-L263
	# We're going to fix this by writing (and thus caching the names of) the baseclasses of classes first
	# This way, we'll be able to know the classname and the virtual functions contained therein, 
	# and thus we will know if there is an overload that exists in a subclass
	# This relies on the fact that dicts are ordered in Python 3.7+
	# If you're running Jiang Yang, either get a job or replace wintables with an OrderedDict

	# Same format as linuxtables
	# {classname: VClass(classname, {thisoffs: [vfunc...], ...}, ...})
	wintables = {}
	for classname, ti in winti.items():
		read_tinfo_win(classname, ti, winti, wintables, {})
	
	return wintables

def fix_windows_classname(classname):
	# Double pointers are spaced...
	classnamefix = classname.replace("* *", "**")

	# References/pointers that are const are spaced...
	classnamefix = classnamefix.replace("const &", "const&")
	classnamefix = classnamefix.replace("const *", "const*")

	# And true/false is instead replaced with 1/0
	def replacer(m):
		# Avoid replacing 1s and 0s that are a part of classnames
		# Thanks ChatGPT
		return re.sub(r"(?<=\W)1(?=\W)", "true", re.sub(r"(?<=\W)0(?=\W)", "false", m.group()))
	classnamefix = re.sub(r"<[^>]+>", replacer, classnamefix)

	# Other quirks are inline structs and templated enums
	# which are pretty much impossible to deduce
	return classnamefix

# Idk why but sometimes pointers have a mind of their own
def fix_windows_classname2(classname):
	return classname.replace(" *", "*")

def fix_win_overloads(linuxitems, winitems, vclass, functable):
	for i in range(min(len(linuxitems), len(winitems))):
		currfuncs = linuxitems[i].funcs
		vfuncs = []
		for u in range(len(currfuncs)):
			f = make_vfunc(mangledname=currfuncs[u])
			pname = f.postname
			for baseclass in vclass.baseclasses.values():
				if pname in baseclass.postnames:
					f.inherited = True
					break
			vfuncs.append(f)

		# Remove Linux's extra dtor
		for u, f in enumerate(vfuncs):
			if "::~" in f.name:
				del vfuncs[u]
				break

		# Windows does overloads backwards, reverse them
		funcnameset = set()
		u = 0
		while u < len(vfuncs):
			f = vfuncs[u]
			if f.inherited:
				u += 1
				continue

			if f.mangledname.startswith("__cxa"):# or f.mangledname.startswith("_ZThn") or f.mangledname.startswith("_ZTv"):
				u += 1
				continue

			if not f.name:
				u += 1
				continue

			# This is an overload, we take the function name here, and push it somewhere else
			if f.sname in funcnameset:
				# Find the first index of the overload
				firstidx = -1
				for k in range(u):
					if vfuncs[k].sname == f.sname:
						firstidx = k
						break

				if firstidx == -1:
					print(f"[VTABLE IO] An impossibility has occurred. \"{f.sname}\" ({f.mangledname}, {f.name}) is in funcnameset but there is no possible overload.")

				# Remove the current func from the list
				del vfuncs[u]
				# And insert it into the first index
				vfuncs.insert(firstidx, f)
				u += 1
				continue

			funcnameset.add(f.sname)
			u += 1

		for f in vfuncs:
			vclass.postnames.add(f.postname)
		functable[linuxitems[i].thisoffs] = vfuncs

def thunk_dance(winitems, vclass, functable):
	# Now it's time for thunk dancing
	mainltable = functable[0]
	mainwtable = winitems[0].funcs
	for currlinuxitems, currwinitems in zip(functable.items(), winitems):
		thisoffs, ltable = currlinuxitems
		wtable = currwinitems.funcs
		if thisoffs == 0:
			continue

		# Remove any extra dtors from this table
		dtorcount = 0
		for i, f in enumerate(ltable):
			if "::~" in f.name:
				dtorcount += 1
				if dtorcount > 1:
					del ltable[i]
					break

		i = 0
		while i < len(mainltable):
			f = mainltable[i]
			if f.mangledname.startswith("__cxa"):
				i += 1
				continue

			# I shouldn't need to do this, but destructors are wonky
			if i == 0 and "::~" in f.name:
				i += 1
				continue

			if not f.postname:
				i += 1
				continue

			# Windows skips the vtable function if it's implementation is in the thunks
			# A way to check if this is true is to see which thunks are actually thunks
			# Then we just pop its name from the main table, since it's no longer there
			thunkidx = -1
			for u in range(len(ltable)):
				if ltable[u].postname == f.postname:
					thunkidx = u
					break

			if thunkidx != -1:
				try:
					# We can't exactly see if the possible thunk jumps to a certain function (mainwtable[i]) because
					# it's impossible to know what that function even is, so we instead check to see if
					# it jumps into any function in the main vtable which is good enough
					if not is_thunk(wtable[thunkidx], mainwtable):
						ltable[thunkidx] = mainltable[i]
						del mainltable[i]
						continue
				except:
					print(f"[VTABLE IO] Anomalous thunk: {vclass.name}::{f.postname}, mainwtable {len(mainwtable)} wtable {len(wtable)} thunkidx {thunkidx} thisoffs {thisoffs}")
					pass
			i += 1
		
		# Update current linux table
		functable[thisoffs] = ltable

	# Update main table
	functable[0] = mainltable

def prep_linux_vtables(linuxitems, winitems, vclass):
	functable = {}

	fix_win_overloads(linuxitems, winitems, vclass, functable)

	# No thunks, we are done
	if min(len(linuxitems), len(winitems)) == 1:
		if len(functable[0]) != len(winitems[0].funcs):
			print(f"[VTABLE IO] WARNING: {vclass.name} vtable may be wrong! L{len(functable[0])} - W{len(winitems[0].funcs)} = {len(functable[0]) - len(winitems[0].funcs)}")
		return functable
	
	thunk_dance(winitems, vclass, functable)

	# Check for any size mismatches
	for items in zip(functable.items(), winitems):
		currlinuxitems, currwinitems = items
		thisoffs, ltable = currlinuxitems
		if len(ltable) != len(currwinitems.funcs):
			print(f"[VTABLE IO] WARNING: {vclass.name} vtable [W{currwinitems.thisoffs}/L{thisoffs}] may be wrong! L{len(ltable)} - W{len(currwinitems.funcs)} = {len(ltable) - len(currwinitems.funcs)}")

	# Ready to write
	return functable

def merge_tables(functable, winitems):
	for items in zip(functable.items(), winitems):
		# Should probably make this unpacking/packing more efficient
		currlitems, currwitems = items
		_, ltable = currlitems
		wtable = currwitems.funcs

		for i, f in enumerate(ltable):
			targetname = f.mangledname
			# Purecall, which should already be handled on the Windows side
			if targetname.startswith("__cxa"):
				continue

			# Size mismatch, ignore it
			try:
				currfunc = wtable[i]
			except:
				continue
			targetaddr = currfunc.ea

			flags = idaapi.get_full_flags(targetaddr)
			# Already typed
			if idaapi.has_name(flags):
				continue

			func = idaapi.get_func(targetaddr)
			# Not actually a function somehow
			if not func:
				continue

			# A library function (should already have a name)
			if func.flags & idaapi.FUNC_LIB:
				continue

			idaapi.set_name(targetaddr, targetname, idaapi.SN_FORCE)
			global FUNCS
			FUNCS += 1

def compare_tables(wintables, linuxtables):
	for classname, vclass in wintables.items():
		if not vclass.vfuncs:
			continue

		linuxtable = linuxtables.get(classname, {})
		if not linuxtable:
			# Some weird Windows quirks
			classnamefix = fix_windows_classname(classname)
			linuxtable = linuxtables.get(classnamefix, {})
			if not linuxtable:
				# Another very weird quirk
				classnamefix = fix_windows_classname2(classnamefix)
				linuxtable = linuxtables.get(classnamefix, {})
				if not linuxtable:
#					print(f"[VTABLE IO] {classname}{f' (tried {classnamefix})' if classname != classnamefix else ''} not found in Linux tables. Skipping...")
					continue

		winitems = list(FuncList(x[0], x[1]) for x in vclass.vfuncs.items())
		# Sort by thisoffs, smallest first
		winitems.sort(key=lambda x: x.thisoffs)

		# Convert the string thisoffs to int
		# Linux thisoffses are negative, abs them
		linuxitems = list(FuncList(abs(int(x[0])), x[1]) for x in zip([abs(int(i)) for i in linuxtable.keys()], linuxtable.values()))
		linuxitems.sort(key=lambda x: x.thisoffs)

		# If there's a size mismatch (very rare), then most likely IDA failed to analyze
		# A certain vtable, so we can't continue given the high probability of catastrophich failure
		if len(winitems) != len(linuxitems):
			print(f"[VTABLE IO] {classname} vtable # mismatch - L{len(linuxitems)} W{len(winitems)}. Skipping...")
			continue

		functable = prep_linux_vtables(linuxitems, winitems, vclass)

		# Write!
		merge_tables(functable, winitems)

def write_vtables():
	importfile = idaapi.ask_file(0, "*.json", "Select a file to import from")
	if not importfile:
		return

# 	global EXPORT_MODE
# 	EXPORT_MODE = idaapi.ask_buttons("Yes", "Export only (do not type functions)", "No", -1, "Would you like to export virtual tables to a file?")

# 	if EXPORT_MODE in (Export_Yes, Export_YesOnly):
# 		exportfile = idaapi.ask_file(1, "*.json", "Select a file to export virtual tables to")
# 		if not exportfile:
# 			return

	WAITBOX.show("Importing file")
	with open(importfile) as f:
		linuxtables = json.load(f)

	WAITBOX.show("Parsing Windows typeinfo")
	winti = read_ti_win()

	WAITBOX.show("Generating windows vtables")
	wintables = gen_win_tables(winti)

	WAITBOX.show("Comparing vtables")
	compare_tables(wintables, linuxtables)

	# if EXPORT_MODE in (Export_Yes, Export_YesOnly):
	# 	WAITBOX.show("Writing to file")
	# 	with open(exportfile, "w") as f:
	# 		json.dump(EXPORT_TABLE, f, indent=4, sort_keys=True)

def main():
	os = get_os()
	if os == -1:
		print(f"Unsupported OS?: {idaapi.get_file_type_name()}")
		idaapi.beep()
		return

	try:
		if os == OS_Linux:
			read_vtables_linux()
			print("Done!")
		elif os == OS_Win:
			write_vtables()
			if FUNCS:
				print("Successfully typed {} virtual functions".format(FUNCS))
			else:
				print("No functions were typed")
				idaapi.beep()
	except:
		import traceback
		traceback.print_exc()
		print("Please file a bug report with supporting information at https://github.com/Scags/IDA-Scripts/issues")
		idaapi.beep()

	WAITBOX.hide()

# import cProfile
# cProfile.run("main()", "vtable_io.prof")
main()