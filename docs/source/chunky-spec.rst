######################################
Chunky (Unofficial) Specification V1.1
######################################

********************
Specification Tables
********************
.. csv-table:: Relic Chunky (V1.1) Specification
    :file: chunky-spec.csv
    :widths: 30, 12, 12, 23, 23
    :header-rows: 1


.. csv-table:: Chunk (V1.1) Specification
    :file: chunk-spec.csv
    :widths: 30, 12, 12, 23, 23
    :header-rows: 1

********************
Chunky Specification
********************
A Relic Chunky file is a Chunk Based file format, which stores data in a special tree structure.

Magic Word
==========
Every Relic Chunky file begins with 12 bytes containing `Relic Chunky` encoded into ascii, followed by 4 bytes of `\\r\\n\\x1a\\0`.

`Relic Chunky` is a magic word, which is always present at the beginning of `Relic Chunky` files, if this is missing, chances are the file is not a proper Relic Chunky file.

`\\r\\n\\x1a\\0` is always present after `Relic Chunky`, but it may not be part of the Magic Word. It's purpose is to allow most command line tools that peek a file for magic words to read the `Relic Chunky` magic word.

Because it is always present, this specification assumes it is a required part of the Magic Word.

Version
=======
The version of the Relic Chunky file. As this is a specification for V1.1, this value should always be 1.

Platform
========
The platform code of the Relic Chunky file. Due to a lack of files from other platforms, the only known code for this is 1.
This specification may not work for other platform codes.

Content
=======
A chunky file does not specify a Content Size, like it's chunks, instead, the remainder of the file is assumed to be Chunks.

See Assembling the Chunky Tree for more information.


*******************
Chunk Specification
*******************
Chunks act as files and folders containing

Chunk Type
==========
There are only two chunk types; Data (DATA) chunks, and Folder (FOLD) chunks.
The chunk type determines how we unpack a Chunk's Content.

See Assembling the Chunky Tree for more information.

Chunk FourCC
============
This is a four character code which can be described as the 'file extension' of the chunk.
Chunks with the same FourCC, Chunk Type, and Version, allowing further parsing of the chunky into usable assets.

Version
============
The version of the chunk, this is allows extractors to properly parse the Chunk's Content when it's layout changes.

Content Size
============
The size of the content buffer.

Name Size
============
The size of the name buffer.
This is typically 0, as chunks do not normally have names unless their are multiple with the same FourCC.

Name
============
The name of the chunk.
This is typically blank, as name size is typically 0.

Content
=============
The actual data of the chunk.

If the chunk is a Data Chunk, this is asset data.
If the chunk is a Folder Chunk, this is a buffer containing more chunks.

See Assembling the Chunky Tree for more information.

**************************
Assembling the Chunky Tree
**************************
#. Read the Chunky Header.
#. Read from the Current Content Buffer.
    #. Read a Chunk Header.
    #. If the Chunk is a Data Chunk.
        #. Chunk is done, attach as leaf of parent.
    #. If the Chunk is a Folder Chunk.
        #. Create a leaf of parent, and set it as the current parent.
        #. Continue from Step 2 (Read from the Folder's Content Buffer).
        #. Set previous parent as current parent.
#. Once all data is read, the Tree is complete.