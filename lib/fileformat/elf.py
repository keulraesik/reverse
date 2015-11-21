#!/usr/bin/env python3
#
# Reverse : Generate an indented asm code (pseudo-C) with colored syntax.
# Copyright (C) 2015    Joel
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.    See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.    If not, see <http://www.gnu.org/licenses/>.
#

import struct
import bisect

from elftools.elf.elffile import ELFFile
from elftools.elf.constants import SH_FLAGS

from lib.utils import warning
from lib.fileformat.binary import SectionAbs


# SHF_WRITE=0x1
# SHF_ALLOC=0x2
# SHF_EXECINSTR=0x4
# SHF_MERGE=0x10
# SHF_STRINGS=0x20
# SHF_INFO_LINK=0x40
# SHF_LINK_ORDER=0x80
# SHF_OS_NONCONFORMING=0x100
# SHF_GROUP=0x200
# SHF_TLS=0x400
# SHF_MASKOS=0x0ff00000
# SHF_EXCLUDE=0x80000000
# SHF_MASKPROC=0xf0000000


class ELF:
    def __init__(self, classbinary, filename):
        import capstone as CAPSTONE

        fd = open(filename, "rb")
        self.elf = ELFFile(fd)
        self.classbinary = classbinary

        self.arch_lookup = {
            "x86": CAPSTONE.CS_ARCH_X86,
            "x64": CAPSTONE.CS_ARCH_X86,
            "ARM": CAPSTONE.CS_ARCH_ARM,
            "MIPS": CAPSTONE.CS_ARCH_MIPS,
        }

        self.arch_mode_lookup = {
            "x86": CAPSTONE.CS_MODE_32,
            "x64": CAPSTONE.CS_MODE_64,
            "ARM": CAPSTONE.CS_ARCH_ARM,
            "MIPS": {
                32: CAPSTONE.CS_MODE_MIPS32,
                64: CAPSTONE.CS_MODE_MIPS64,
            }
        }

        self.__sections = {} # start address -> elf section

        for s in self.elf.iter_sections():
            start = s.header.sh_addr

            if s.header.sh_flags & 0xf != 0:
                bisect.insort_left(classbinary._sorted_sections, start)

            self.__sections[start] = s
            is_data = self.__section_is_data(s)
            is_exec = self.__section_is_exec(s)

            if is_data:
                data = s.data()
            else:
                data = None

            classbinary._abs_sections[start] = SectionAbs(
                    s.name.decode(),
                    start,
                    s.header.sh_size,
                    s.header.sh_size,
                    is_exec,
                    is_data,
                    data)


    def load_section_names(self):
        # Used for the auto-completion
        for s in self.elf.iter_sections():
            if s.header.sh_flags & 0xf != 0:
                ad = s.header.sh_addr
                name = s.name.decode()
                self.classbinary.section_names[name] = ad


    def load_static_sym(self):
        symtab = self.elf.get_section_by_name(b".symtab")
        if symtab is None:
            return
        dont_save = [b"$a", b"$t", b"$d"]
        arch = self.elf.get_machine_arch()
        is_arm = arch == "ARM"
        for sy in symtab.iter_symbols():
            if is_arm and sy.name in dont_save:
                continue
            if sy.entry.st_value != 0 and sy.name != b"":
                self.classbinary.reverse_symbols[sy.entry.st_value] = sy.name.decode()
                self.classbinary.symbols[sy.name.decode()] = sy.entry.st_value


    def __x86_resolve_reloc(self, rel, symtab, plt, got_plt, addr_size):
        # Save all got offsets with the corresponding symbol
        got_off = {}
        for r in rel.iter_relocations():
            sym = symtab.get_symbol(r.entry.r_info_sym)
            name = sym.name.decode()
            if name and r.entry.r_offset:
                got_off[r.entry.r_offset] = name + "@plt"

        data = got_plt.data()

        unpack_str = "<" if self.elf.little_endian else ">"
        unpack_str += str(int(len(data) / addr_size))
        unpack_str += "Q" if addr_size == 8 else "I"

        got_values = struct.unpack(unpack_str, data)
        plt_data = plt.data()
        wrong_jump_opcode = False
        off = got_plt.header.sh_addr

        # Read the .got.plt and for each address in the plt, substract 6
        # to go at the begining of the plt entry.

        opcode_jmp = [b"\xff\x25", b"\xff\xa3"]

        for jump_in_plt in got_values:
            if off in got_off:
                plt_start = jump_in_plt - 6
                plt_off = plt_start - plt.header.sh_addr

                # Check "jmp *(ADDR)" opcode.
                if plt_data[plt_off:plt_off+2] not in opcode_jmp:
                    wrong_jump_opcode = True
                    continue

                name = got_off[off]
                self.classbinary.reverse_symbols[plt_start] = name
                self.classbinary.symbols[name] = plt_start

            off += addr_size

        if wrong_jump_opcode:
            warning("I'm expecting to see a jmp *(ADDR) on each plt entry")
            warning("opcode \\xff\\x25 was not found, please report")


    def __resolve_symtab(self, rel, symtab):
        # TODO: don't know why st_value is not 0 like x86
        # In some executables I've tested, it seems that st_value
        # is the address of the plt entry

        # TODO: really useful to iter on relocations and get the symbol
        # from the symtab ?
        # for r in rel.iter_relocations():
            # sym = symtab.get_symbol(r.entry.r_info_sym)

        for sym in symtab.iter_symbols():
            plt_start = sym.entry.st_value
            if plt_start != 0:
                name = sym.name.decode()
                self.classbinary.reverse_symbols[plt_start] = name
                self.classbinary.symbols[name] = plt_start


    def __iter_reloc(self):
        for rel in self.elf.iter_sections():
            if rel.header.sh_type in ["SHT_RELA", "SHT_REL"]:
                symtab = self.elf.get_section(rel.header.sh_link)
                if symtab is None:
                    continue
                yield (rel, symtab)


    def load_dyn_sym(self):
        arch = self.elf.get_machine_arch()

        if arch == "ARM" or arch == "MIPS":
            for (rel, symtab) in self.__iter_reloc():
                self.__resolve_symtab(rel, symtab)
            return

        # x86/x64

        # TODO: .plt can be renamed ?
        plt = self.elf.get_section_by_name(b".plt")

        if plt is None:
            warning(".plt section not found")
            return

        # TODO: .got.plt can be renamed or may be removed ?
        got_plt = self.elf.get_section_by_name(b".got.plt")
        addr_size = 8 if arch == "x64" else 4

        if got_plt is None:
            warning(".got.plt section not found")
            return

        for (rel, symtab) in self.__iter_reloc():
            self.__x86_resolve_reloc(rel, symtab, plt, got_plt, addr_size)


    def __section_is_data(self, s):
        mask = SH_FLAGS.SHF_WRITE | SH_FLAGS.SHF_ALLOC
        return s.header.sh_flags & mask and not self.__section_is_exec(s)


    def __section_is_exec(self, s):
        if s is None:
            return 0
        return s.header.sh_flags & SH_FLAGS.SHF_EXECINSTR


    def section_stream_read(self, addr, size):
        s = self.classbinary.get_section(addr)
        if s is None:
            return b""
        s = self.__sections[s.start]
        off = addr - s.header.sh_addr
        end = s.header.sh_addr + s.header.sh_size
        s.stream.seek(s.header.sh_offset + off)
        return s.stream.read(min(size, end - addr))


    def get_arch(self):
        import capstone as CAPSTONE
        arch = self.arch_lookup.get(self.elf.get_machine_arch(), None)
        mode = self.arch_mode_lookup.get(self.elf.get_machine_arch(), None)

        if arch is None:
            return None, None

        # If one arch name has multiple "word size"
        if isinstance(mode, dict):
            mode = mode[self.elf.elfclass]

        if self.elf.little_endian:
            mode |= CAPSTONE.CS_MODE_LITTLE_ENDIAN
        else:
            mode |= CAPSTONE.CS_MODE_BIG_ENDIAN

        return arch, mode


    def get_arch_string(self):
        return self.elf.get_machine_arch()


    def get_entry_point(self):
        return self.elf.header['e_entry']
