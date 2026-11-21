import sys
s = open(sys.argv[1]).read()
a = s.index("#define PEX_RST_PIN")
b = s.index("// Restore the stock control path")
body = s[a:b]
c0 = body.index("// ------------------------------------------------------------------ clocks")
c1 = body.index("// ------------------------------------------------------------- card control")
open(sys.argv[2], "w").write('#include "board.h"\n' + body[:c0] + body[c1:])
