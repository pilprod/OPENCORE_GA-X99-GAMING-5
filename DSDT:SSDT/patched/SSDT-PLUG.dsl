DefinitionBlock ("", "SSDT", 2, "CORP", "CpuPlug", 0x00003000)
{
    External (\_SB.SCK0.PR00, ProcessorObj)

    Method (PMPM, 4, NotSerialized) {
       If (LEqual (Arg2, Zero)) {
           Return (Buffer (One) { 0x03 })
       }
       Return (Package (0x02)
       {
           "plugin-type", 
           One
       })
    }

    Scope (\_SB.SCK0.PR00)
    {
        If (_OSI ("Darwin")) {
            Method (_DSM, 4, NotSerialized)  // _DSM: Device-Specific Method
            {
                Return (PMPM (Arg0, Arg1, Arg2, Arg3))
            }
        }
    }
}