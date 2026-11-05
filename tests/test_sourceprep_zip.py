# SPDX-License-Identifier: BSD-2-Clause
# Origin: EmberBSD contributors (AI-assisted).
"""ZIP source extraction follows the same archive safety contract as tar."""
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.dont_write_bytecode = True
SDK_SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_SOURCE / 'src'))

from emberbsd_sdk import sourceprep as sp
from emberbsd_sdk.errors import SDKError

class ZipExtractionTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(prefix='sdk-zip-draft-'); self.root=Path(self.temp.name); self.archive=self.root/'src.zip'; self.out=self.root/'out';self.out.mkdir()
 def tearDown(self): self.temp.cleanup()
 def pack(self,rows):
  with zipfile.ZipFile(self.archive,'w') as z:
   for name,data,mode in rows:
    entry=zipfile.ZipInfo(name,(2020,1,2,3,4,6));entry.create_system=3;entry.external_attr=mode<<16;z.writestr(entry,data)
 def extract(self,**kw):sp.extract_archive(self.archive,self.out,'source','fixture',**kw)
 def test_regular_and_executable(self):
  self.pack([('source/bin/tool',b'tool',stat.S_IFREG|0o755),('source/input',b'data',stat.S_IFREG|0o644)])
  self.extract();self.assertEqual((self.out/'source/input').read_bytes(),b'data');self.assertEqual((self.out/'source/bin/tool').stat().st_mode&0o777,0o755)
 def test_refuse_collisions_and_unsafe(self):
  for rows in ([('../escape',b'x',stat.S_IFREG|0o644)], [('source/A/x',b'x',stat.S_IFREG|0o644),('source/a/y',b'y',stat.S_IFREG|0o644)], [('source/f',b'x',stat.S_IFREG|0o644),('source/f',b'y',stat.S_IFREG|0o644)], [('source/f',b'x',stat.S_IFIFO|0o644)]):
   with self.subTest(rows=rows):
    self.pack(rows)
    with self.assertRaises(SDKError): self.extract()
    import shutil;shutil.rmtree(self.out);self.out.mkdir()
 def test_limits_and_selection(self):
  self.pack([('source/keep',b'abc',stat.S_IFREG|0o644),('source/drop',b'def',stat.S_IFREG|0o644)])
  with self.assertRaises(SDKError):self.extract(max_members=1)
  import shutil;shutil.rmtree(self.out);self.out.mkdir()
  with self.assertRaises(SDKError):self.extract(max_bytes=2)
  shutil.rmtree(self.out);self.out.mkdir();self.extract(components=('keep',));self.assertTrue((self.out/'source/keep').exists());self.assertFalse((self.out/'source/drop').exists())
 def test_symlinks(self):
  self.pack([('source/item',b'x',stat.S_IFREG|0o644),('source/alias',b'item',stat.S_IFLNK|0o777)])
  with self.assertRaises(SDKError):self.extract()
  import shutil;shutil.rmtree(self.out);self.out.mkdir();self.extract(internal_symlinks=True);self.assertEqual((self.out/'source/alias').read_bytes(),b'x')
  shutil.rmtree(self.out);self.out.mkdir();self.pack([('source/alias',b'../../escape',stat.S_IFLNK|0o777)])
  with self.assertRaises(SDKError):self.extract(internal_symlinks=True)
 def test_nul_central_name(self):
  self.pack([('source/aXb',b'data',stat.S_IFREG|0o644)])
  data=self.archive.read_bytes().replace(b'source/aXb',b'source/a\x00b')
  self.archive.write_bytes(data)
  with self.assertRaises(SDKError): self.extract()
 def test_bad_crc_compression_and_encryption(self):
  import struct, shutil
  for kind in ('crc','compression','encrypted'):
   with self.subTest(kind=kind):
    self.pack([('source/file',b'data',stat.S_IFREG|0o644)])
    data=bytearray(self.archive.read_bytes()); local=data.index(b'PK\x03\x04'); central=data.index(b'PK\x01\x02')
    if kind=='crc':
     filename_len, extra_len=struct.unpack_from('<HH',data,local+26)
     data[local+30+filename_len+extra_len] ^= 1
    elif kind=='compression':
     struct.pack_into('<H',data,local+8,99);struct.pack_into('<H',data,central+10,99)
    else:
     struct.pack_into('<H',data,local+6,1);struct.pack_into('<H',data,central+8,1)
    self.archive.write_bytes(data)
    with self.assertRaises(SDKError):self.extract()
    shutil.rmtree(self.out);self.out.mkdir()

if __name__ == '__main__':
 unittest.main()
