import os, sys, re, json, binascii, shutil
from Scripts import run, utils, ioreg, plist, reveal
from collections import OrderedDict

class USBMap:
    def __init__(self):
        os.chdir(os.path.dirname(os.path.realpath(__file__)))
        self.u = utils.Utils("USBMap")
        # Verify running os
        if not sys.platform.lower() == "darwin":
            self.u.head("Wrong OS!")
            print("")
            print("USBMap can only be run on macOS!")
            print("")
            self.u.grab("Press [enter] to exit...")
            exit()
        self.r = run.Run()
        self.i = ioreg.IOReg()
        self.re = reveal.Reveal()
        self.map_hubs = False # Enable to show hub ports/devices in mapping
        self.map_xhci_hubs = False # Enable to create maps for USB 2 hubs on XHCI controllers
        self.controllers = None
        self.smbios = None
        self.os_build_version = "Unknown"
        self.os_version = "0.0.0"
        self.usb_port = re.compile("Apple[a-zA-Z0-9]*USB\d*[A-Z]+Port,")
        self.usb_cont = re.compile("Apple[a-zA-Z0-9]*USB[A-Z]+,")
        self.usb_hub  = re.compile("Apple[a-zA-Z0-9]*USB\d+Hub,")
        self.usb_hubp = re.compile("Apple[a-zA-Z0-9]*USB\d+HubPort,")
        self.usb_ext  = [
            re.compile("<class IOBluetoothHostControllerUSBTransport,"), # Custom match for orphaned bt devices (Intel/Atheros/etc)
            re.compile("^(?!.*IOUSBHostDevice@).*<class IOUSBHostDevice,") # Matches IOUSBHostDevice classes that are *not* named IOUSBHostDevice (avoids entry spam in discovery)
        ] # List of extra objects to match against
        self.map_list = self.get_map_list()
        self.discover_wait = 5
        self.default_names = ("XHC1","EHC1","EHC2","PXSX")
        self.cs = u"\u001b[32;1m"
        self.ce = u"\u001b[0m"
        self.bs = u"\u001b[36;1m"
        self.rs = u"\u001b[31;1m"
        self.nm = u"\u001b[35;1m"
        self.ioreg = self.populate_ioreg()
        self.by_ioreg = None
        self.usb_list = "./Scripts/USB.plist"
        self.output   = "./Results"
        self.ssdt_path = os.path.join(self.output,"SSDT-USB-Reset.dsl")
        self.rsdt_path = os.path.join(self.output,"SSDT-RHUB-Reset.dsl")
        self.kext_path = os.path.join(self.output,"USBMap.kext")
        self.info_path = os.path.join(self.kext_path,"Contents","Info.plist")
        self.merged_list = OrderedDict()
        # Load the USB list as needed
        if os.path.exists(self.usb_list):
            try:
                with open(self.usb_list,"rb") as f:
                    self.merged_list = plist.load(f,dict_type=OrderedDict)
            except: pass
        if not isinstance(self.merged_list,dict): self.merged_list = OrderedDict()
        self.check_controllers()
        self.connected_controllers = self.populate_controllers()
        # Get illegal names
        self.plugin_path = "/System/Library/Extensions/IOUSBHostFamily.kext/Contents/PlugIns"
        self.illegal_names = self.get_illegal_names()

    def get_illegal_names(self):
        if not self.smbios or not os.path.exists(self.plugin_path):
            return [x for x in self.default_names] # No SMBIOS, fall back on defaults
        illegal_names = ["PXSX"] # Always start with the default PXSX name
        for plugin in os.listdir(self.plugin_path):
            plug_path = os.path.join(self.plugin_path,plugin)
            info_path = os.path.join(plug_path,"Contents","Info.plist")
            if plugin.startswith(".") or not os.path.isdir(plug_path): continue # Skip invisible or non-directories
            # Got a valid directory - let's check the Info.plist
            if not os.path.exists(info_path): continue # Doesn't exist
            # Try to load, then walk the structure
            try:
                with open(info_path,"rb") as f:
                    plist_data = plist.load(f)
            except: continue # Borked Info.plist - skip
            for key in plist_data:
                if not key.startswith("IOKitPersonalities"): continue
                # Got the proper key, let's walk the structure
                walk_dict = plist_data[key]
                for k in walk_dict:
                    # Find out if we have a model and IONameMatch here
                    smbios_entry = walk_dict[k]
                    if not all((x in smbios_entry for x in ("model","IONameMatch"))): continue # No matches
                    # Got both - let's see if the SMBIOS is ours
                    if not smbios_entry["model"] == self.smbios: continue # Mismatch, skip
                    # Take note of the IONameMatch, and add it to the illegal_names list
                    illegal_names.append(smbios_entry["IONameMatch"])
        return sorted(list(set(illegal_names)))

    def get_map_list(self):
        map_list = [self.usb_cont,self.usb_port]+self.usb_ext
        if self.map_hubs: map_list.extend([self.usb_hub,self.usb_hubp])
        return map_list

    def get_matching_controller(self,controller_name,from_cont=None,into_cont=None):
        self.check_controllers()
        from_cont = from_cont if from_cont != None else self.merged_list
        into_cont = into_cont if into_cont != None else self.controllers
        assert controller_name in from_cont # Can't match if it doesn't exist!
        if controller_name in into_cont:
            return controller_name # The same name@address exist - should be the same entry
        if "locationid" in from_cont[controller_name]:
            # If it has a location - we want to match that
            cont_adj = next((x for x in into_cont if from_cont[controller_name]["locationid"] == into_cont[x].get("locationid",None)),None)
            if cont_adj: return cont_adj
        # Let's try by matching the ACPI path?
        cont_adj = next((x for x in into_cont if from_cont[controller_name].get("acpi_path",None) == into_cont[x].get("acpi_path","Unknown")),None)
        if cont_adj: return cont_adj
        # Try by name only?
        return next((x for x in into_cont if controller_name.split("@")[0] == x.split("@")[0]),None)

    def merge_controllers(self,from_cont=None,into_cont=None):
        self.check_controllers()
        from_cont = from_cont if from_cont != None else self.merged_list
        into_cont = into_cont if into_cont != None else self.controllers
        # Helper function to combine from_cont's settings with into_cont's
        for cont in from_cont:
            # Skip any that don't exist - controllers don't materialize out of nothing
            # They do apparently change addresses though - so try to match by name... this *hopes* that
            # the users has setup unique names for all controllers first.
            cont_adj = self.get_matching_controller(cont,from_cont,into_cont)
            if not cont_adj: continue
            # Walk its settings and add them
            for key in from_cont[cont]:
                if key == "ports": continue # Skip this for afterward to merge the ports individually
                into_cont[cont_adj][key] = from_cont[cont][key] # Force override and stuff
            for port_num in from_cont[cont]["ports"]:
                port = from_cont[cont]["ports"][port_num]
                mort = into_cont[cont_adj].get("ports",{}).get(port_num,{})
                # Let's walk the keys
                for key in port:
                    # Merge item lists as sets to avoid duplicates
                    if key == "items":
                        new_items = mort.get("items",[])
                        new_items.extend([x for x in port["items"] if not x in mort.get("items",[])])
                        mort["items"] = new_items
                    elif key in ("name","id"): continue # Skip the name and id to always use the most recent
                    else: mort[key] = port[key]
        return into_cont

    def save_plist(self,controllers=None):
        if controllers == None: controllers = self.merged_list
        # Ensure the lists are the same
        try:
            with open(self.usb_list,"wb") as f:
                plist.dump(controllers,f,sort_keys=False)
            return True
        except Exception as e:
            print("Could not save to USB.plist! {}".format(e))
        return False

    def populate_ioreg(self):
        if os.path.exists("ioreg.txt"):
            with open("ioreg.txt","rb") as f:
                ioreg = f.read().decode("utf-8",errors="ignore").split("\n")
                self.i.ioreg = {"IOService":ioreg}
            return ioreg
        else:
            return self.i.get_ioreg() 

    def check_controllers(self):
        if not self.controllers: self.controllers = self.populate_controllers()
        assert self.controllers # Error if it's not populated after forcing
        return self.controllers

    def check_by_ioreg(self,force=False):
        if force or not self.by_ioreg: self.by_ioreg = self.get_by_ioreg()
        assert self.by_ioreg # Error if it's not populated after updating
        return self.by_ioreg

    def get_obj_from_line(self, line):
        # Breaks a line into usable components - returns a dict on success, None on error
        try:
            return {
                "line":line,
                "indent":len(line)-len(line.lstrip()),
                "id": line.split("id ")[-1],
                "name":line.lstrip().split("  <class")[0],
                "type":line.split("<class ")[1].split(",")[0],
                "items":{}
            }
        except Exception as e:
            print(e)
        return None # Bad values - bail

    def get_by_ioreg(self):
        # Get a dict of all populated ports and their AppleUSBDevices
        if os.path.exists("ioreg.txt"):
            with open("ioreg.txt","rb") as f:
                ioreg = f.read().decode("utf-8",errors="ignore")
        else:
            ioreg = self.r.run({"args":["ioreg","-c","IOUSBDevice","-w0"]})[0]
        # Trim the list down to only what we want
        valid = [x.replace("|"," ").replace("+-o ","").split(", registered")[0] for x in ioreg.split("\n") if any((y.search(x) for y in self.map_list))]
        # Initialize our dict
        ports = {"items":{}}
        for index,wline in enumerate(valid):
            # Walk until we find a valid device, then walk backward to figure out the pathing
            if any((x.search(wline) for x in self.usb_ext)):
                obj = self.get_obj_from_line(wline)
                if not obj: continue # bad value - skip
                path = [obj]
                last_indent = obj["indent"]
                # Walk in reverse, keeping track of only port/hub/controller objects that are less indented
                for line in valid[index::-1]:
                    obj = self.get_obj_from_line(line)
                    if not obj: continue # bad value - skip
                    if obj["indent"] >= last_indent: continue # Only check for parent objects
                    # Reset our indent to ensure the next check
                    last_indent = obj["indent"]
                    # if self.usb_cont.search(line) or self.usb_port.search(line) or self.usb_hub.search(line) or self.usb_hubp.search(line):
                    # We got a USB port, hub, or controller - add it
                    path.append(obj)
                    if self.usb_cont.search(line): break # We hit a controller, break out to avoid nesting under another
                # Reverse the path order to reflect top-level elements
                path = path[::-1]
                if self.map_xhci_hubs: map_hub = True # Always allow for debugging
                else: map_hub = not any(("XHCI" in x["type"] for x in path)) # Aggregate XHCI hubs under the parent ports
                # Walk the paths, and add them by id to the ports dict
                last_root = ports
                # Iterate each path element and ensure it exists in the ports dict
                for p in path:
                    # Check the type to see if we got a device - if so, disable
                    # map_hub to avoid mapping external device hubs
                    if p["type"] == "IOUSBHostDevice": map_hub = False
                    p["map_hub"] = map_hub
                    if not p["id"] in last_root["items"]:
                        # Add it if it doesn't exist
                        last_root["items"][p["id"]] = p
                    # Reset our reference to the current scope
                    last_root = last_root["items"][p["id"]]
        return ports

    def map_inheritance(self,top_level,level=1,indent="    "):
        # Iterates through all "items" entries in the top_level dict
        # and returns a formatted string showing inheritance
        if not "items" in top_level: return []
        text = []
        for v in top_level["items"]:
            check_entry = top_level["items"][v]
            is_hub = self.usb_hub.search(check_entry.get("line","Unknown"))
            try: name,addr = check_entry.get("name","Unknown").split("@")
            except:
                addr = "Unknown"
                name = check_entry.get("name",check_entry.get("type","Unknown"))
            value = (indent * level) + "- {}{}".format(name, " (HUB-{})".format(addr) if check_entry.get("map_hub",False) and is_hub else "")
            text.append(value)
            # Verify if we're on a hub and mapping those
            if check_entry.get("map_hub",False) and is_hub:
                # Got a hub - this will be mapped elsewhere
                continue
            # Check if we have items to map
            if len(check_entry.get("items",[])):
                # We have items!
                text.extend(self.map_inheritance(check_entry,level+1))
        return text

    def get_port_from_dict(self,port_id,top_level):
        if port_id in top_level["items"]: return top_level["items"][port_id]
        for port in top_level["items"]:
            test_port = self.get_port_from_dict(port_id,top_level["items"][port])
            if test_port: return test_port
        return None

    def get_items_for_port(self,port_id,indent="    "):
        port = self.get_port_from_dict(port_id,self.check_by_ioreg())
        if not port: return [] # No items, or the port wasn't found?
        return self.map_inheritance(port)

    def get_ports_and_devices_for_controller(self,controller,indent="    "):
        self.check_controllers()
        assert controller in self.controllers # Error if the controller doesn't exist
        port_dict = OrderedDict()
        for port_num in self.controllers[controller]["ports"]:
            port = self.controllers[controller]["ports"][port_num]
            # The name of each entry should be "PortName - PortNum (Controller)"
            port_num = self.hex_dec(self.hex_swap(port["port"]))
            entry_name = "{} | {} | {} | {} | {}".format(port["name"],port["port"],port["address"],controller,self.controllers[controller]["parent"])
            port_dict[entry_name] = self.get_items_for_port(port["id"],indent=indent)
        return port_dict

    def get_ports_and_devices(self,indent="    "):
        # Returns a dict of all ports and their connected devices
        self.check_controllers()
        port_dict = OrderedDict()
        for x in self.controllers:
            port_dict.update(self.get_ports_and_devices_for_controller(x,indent=indent))
        return port_dict

    def get_populated_count_for_controller(self,controller):
        port_dict = self.get_ports_and_devices_for_controller(controller)
        return len([x for x in port_dict if len(port_dict[x])])

    def populate_controllers(self):
        assert self.ioreg != None # Error if we have no ioreg to iterate
        self.smbios = None
        controllers = OrderedDict()
        if os.path.exists("ioreg.txt"):
            with open("ioreg.txt","rb") as f:
                ioreg = f.read().decode("utf-8",errors="ignore")
        else:
            ioreg = self.r.run({"args":["ioreg","-c","IOUSBDevice","-w0"]})[0]
        # Trim the list down to only what we want
        valid = [x.replace("|"," ").replace("+-o ","").split(", registered")[0] for x in ioreg.split("\n") if any((y.search(x) for y in self.map_list))]
        for index,wline in enumerate(valid):
            # Walk until we find a port, then walk backward to figure out the controller
            if not (self.usb_port.search(wline) or self.usb_hubp.search(wline)): continue
            # We got a port - go backward until we find the controller/hub, but outright bail if we find a device
            obj = self.get_obj_from_line(wline)
            if not obj: continue # bad value - skip
            # Reorganize the dict slightly
            obj["full_name"] = obj["name"]
            obj["address"] = obj["name"].split("@")[-1]
            obj["name"] = obj["full_name"].split("@")[0]
            obj["items"] = []
            # Find its port number first in the full ioservice dump
            port_primed = False
            for line in self.ioreg:
                if port_primed:
                    if line.replace("|","").strip() == "}":
                        break # We hit the end of that port
                    if '"port" = ' in line:
                        obj["port"] = line.split("<")[1].split(">")[0]
                        break
                # Verify by full name and id
                if obj["full_name"] in line and obj["id"] in line:
                    port_primed = True # We found it!
            last_indent = obj["indent"]
            controller = last_hub = None
            # Walk in reverse, keeping track of only port/hub/controller objects that are less indented
            for line in valid[index::-1]:
                check_obj = self.get_obj_from_line(line)
                if not check_obj: continue # Bad data
                if check_obj["indent"] >= last_indent: continue # Only check for parent objects
                # Reset our indent to ensure the next check
                last_indent = check_obj["indent"]
                if any((x.search(line) for x in self.usb_ext)):
                    controller = last_hub = None
                    break # Bail on valid device
                elif self.usb_hub.search(line):
                    # Retain the last-seen hub device
                    last_hub = self.get_obj_from_line(line)
                elif self.usb_cont.search(line):
                    controller = self.get_obj_from_line(line)
                    break # Bail if we hit a controller - as those don't nest
            if not controller: continue # No controller, nothing to do
            # Ensure the top-level controller is listed
            add_name = cont_full = controller["name"]
            cont_name, cont_addr = cont_full.split("@")
            if not cont_full in controllers:
                # Ensure the parent controller exists regardless
                controllers[cont_full] = controller
                controllers[cont_full]["name"] = cont_name
                controllers[cont_full]["address"] = cont_addr
                controllers[cont_full]["ports"] = OrderedDict()
            if last_hub and (not "XHCI" in controller["type"] or self.map_xhci_hubs):
                # We got a hub that we can map
                add_name = "HUB-{}".format(last_hub["name"].split("@")[-1])
                if not add_name in controllers:
                    controllers[add_name] = last_hub
                    controllers[add_name]["is_hub"] = True
                    controllers[add_name]["name"],controllers[add_name]["address"] = last_hub["name"].split("@")
                    controllers[add_name]["locationid"] = self.hex_dec(last_hub["name"].split("@")[-1]) # Only add the location for USB 2 HUBs
                    controllers[add_name]["ports"] = OrderedDict()
            controllers[add_name]["ports"][obj["port"]] = obj
        # Walk the controllers and retain the parent_name, acpi_path, and _ADR values
        parent = parent_name = acpi_path = acpi_addr = None
        for acpi_line in self.ioreg:
            if "<class IOPlatformExpertDevice," in acpi_line:
                self.smbios = acpi_line.split("+-o ")[1].split("<class")[0].strip()
            if '"acpi-path"' in acpi_line:
                acpi_path = acpi_line.split('"')[-2]
                continue
            elif "<class IOPCIDevice," in acpi_line:
                # Let's get the parent name and acpi_addr - it'll look like @1F,3 - but we want it in 0x001F0003 format
                try:
                    parent      = acpi_line.split("+-o ")[1].split("  <class")[0]
                    parent_name,temp_addr = parent.split("@")
                    major,minor = temp_addr.split(",") if "," in temp_addr else temp_addr,"0"
                    acpi_addr   = "0x{}{}".format(major.rjust(4,"0"),minor.rjust(4,"0"))
                    acpi_addr   = "Zero" if acpi_addr == "0x00000000" else acpi_addr
                except Exception as e:
                    acpi_addr = None
                continue
            # Try to get the object@address
            try:
                current_obj = acpi_line.split("+-o ")[1].split("  <class")[0]
            except: continue
            if current_obj in controllers:
                controllers[current_obj]["parent"] = parent
                controllers[current_obj]["parent_name"] = parent_name
                controllers[current_obj]["acpi_path"] = acpi_path
                controllers[current_obj]["acpi_address"] = acpi_addr if acpi_addr else "Zero"
                # Reset the temp vars
                parent_name = acpi_addr = acpi_path = None
        return controllers

    def build_kext(self):
        self.u.resize(80, 24)
        empty_controllers = []
        skip_empty = True
        for x in self.merged_list:
            ports = self.merged_list[x]["ports"]
            if all((ports[y].get("enabled",False) == False for y in ports)):
                empty_controllers.append(x)
        if len(empty_controllers):
            if all((x in empty_controllers for x in self.merged_list)):
                # No ports selected at all... silly people
                self.u.head("No Ports Selected")
                print("")
                print("There are no ports enabled!")
                print("Please enable at least one port and try again.")
                print("")
                self.u.grab("Press [enter] to return to the menu...")
                return
            while True:
                self.u.head("Controller Validation")
                print("")
                print("Found empty controllers!")
                print("The following controllers have no enabled ports:\n")
                for x in empty_controllers:
                    print(" - {}".format(x))
                print("")
                e = self.u.grab("Choose whether to (i)gnore or (d)isable them: ")
                if not len(e): continue
                if e.lower() in ("i","ignore","d","disable"):
                    skip_empty = e.lower() in ("i","ignore")
                    break
        # Build the kext
        self.u.head("Build USBMap.kext")
        print("")
        os.chdir(os.path.dirname(os.path.realpath(__file__)))
        print("Generating Info.plist...")
        info_plist = self.build_info_plist(skip_empty=skip_empty)
        if os.path.exists(self.kext_path):
            print("Located existing USBMap.kext - removing...")
            shutil.rmtree(self.kext_path,ignore_errors=True)
        print("Creating bundle structure...")
        os.makedirs(os.path.join(self.kext_path,"Contents"))
        print("Writing Info.plist...")
        with open(self.info_path,"wb") as f:
            plist.dump(info_plist,f)
        print("Done.")
        print("")
        self.re.reveal(self.kext_path,True)
        self.u.grab("Press [enter] to return to the menu...")

    def build_info_plist(self,skip_empty=True):
        output_plist = {
            "CFBundleDevelopmentRegion": "English",
            "CFBundleGetInfoString": "v1.0",
            "CFBundleIdentifier": "com.corpnewt.USBMap",
            "CFBundleInfoDictionaryVersion": "6.0",
            "CFBundleName": "USBMap",
            "CFBundlePackageType": "KEXT",
            "CFBundleShortVersionString": "1.0",
            "CFBundleSignature": "????",
            "CFBundleVersion": "1.0",
            "IOKitPersonalities": {}, # Consider IOKitPersonalities_x86_64 on 10.15+
            "OSBundleRequired": "Root"
        }
        for x in self.merged_list:
            ports = self.merged_list[x]["ports"]
            if all((ports[y].get("enabled",False) == False for y in ports)) and skip_empty:
                # Got an empty controller, bail
                continue
            top_port = hs_port = ss_port = 0
            top_data = self.hex_to_data("00000000")
            providers = (
                ("OHCI","AppleUSBOHCIPCI"),
                ("UHCI","AppleUSBUHCIPCI"),
                ("EHCI","AppleUSBEHCIPCI"),
                ("20Hub","AppleUSB20InternalHub"),
                ("XHCI","AppleUSBXHCIPCI")
            )
            new_entry = {
                "CFBundleIdentifier": "com.apple.driver.AppleUSBMergeNub",
                "IOClass": "AppleUSBMergeNub", # Consider AppleUSBHostMergeProperties on 10.15+
                "IONameMatch": self.merged_list[x]["parent_name"],
                # Provider class for OHCI, UHCI, EHCI, USB 2.0 hubs, and XHCI based on controller type - falls back to XHCI on no match
                "IOProviderClass": next((y[1] for y in providers if y[0] in self.merged_list[x]["type"]),"AppleUSBXHCIPCI"),
                "IOProviderMergeProperties": {
                    "kUSBMuxEnabled": False,
                    "port-count": 0,
                    "ports": {}
                },
                "model": self.smbios
            }
            if "locationid" in self.merged_list[x]:
                # We have a hub - save the loc id and up the IOProbeScore - use the most recent though - by the name's address
                try: new_entry["locationID"] = self.hex_dec(x.split("-")[-1])
                except: new_entry["locationID"] = self.merged_list[x]["locationid"] # Fall back on the original locationid
                new_entry["IOProbeScore"] = 5000
                # No need to name match as we're using locationID instead
                new_entry.pop("IONameMatch",None)
            if "XHCI" in self.merged_list[x]["type"]:
                # Only add the kUSBMuxEnabled property to XHCI controllers
                new_entry["IOProviderMergeProperties"]["kUSBMuxEnabled"] = True
            for port_num in self.merged_list[x]["ports"]:
                port = self.merged_list[x]["ports"][port_num]
                # Increment values
                if "USB3" in port["type"]:
                    # All USB 3+ ports are SSxx
                    ss_port += 1
                    port_name = self.get_numbered_name("SS00",ss_port,False)
                else:
                    # USB 2 personalties of XHCI are HSxx, otherwise PRTx
                    hs_port += 1
                    port_name = self.get_numbered_name("HS00" if "XHCI" in self.merged_list[x]["type"] else "PRT0",hs_port,False)
                # Make sure the port is enabled
                if not port.get("enabled",False): continue # Disabled, skip it
                # Check port number
                port_number = self.hex_dec(self.hex_swap(port["port"]))
                if port_number > top_port:
                    top_port = port_number
                    top_data = self.hex_to_data(port["port"])
                # Check port type prioritizing overrides if found
                usb_connector = port.get("type_override", 3 if "XHCI" in self.merged_list[x]["type"] else 0)
                # Add the port with the connector type and port number
                new_entry["IOProviderMergeProperties"]["ports"][port_name] = {
                    "UsbConnector": usb_connector,
                    "port": self.hex_to_data(port["port"])
                }
                # Retain any comments
                if "comment" in port:
                    new_entry["IOProviderMergeProperties"]["ports"][port_name]["Comment"] = port["comment"]
            new_entry["IOProviderMergeProperties"]["port-count"] = top_data # Keep track of the highest port number used
            output_plist["IOKitPersonalities"][self.smbios+"-"+x.split("@")[0]]= new_entry
        return output_plist

    # Helper methods
    def check_hex(self, value):
        # Remove 0x
        return re.sub(r'[^0-9A-Fa-f]+', '', value.lower().replace("0x", ""))

    def hex_to_data(self, value):
        return plist.wrap_data(binascii.unhexlify(self.check_hex(value).encode("utf-8")))

    def hex_swap(self, value):
        input_hex = self.check_hex(value)
        if not len(input_hex): return None
        # Normalize hex into pairs
        input_hex = list("0"*(len(input_hex)%2)+input_hex)
        hex_pairs = [input_hex[i:i + 2] for i in range(0, len(input_hex), 2)]
        hex_rev = hex_pairs[::-1]
        hex_str = "".join(["".join(x) for x in hex_rev])
        return hex_str.upper()

    def hex_dec(self, value):
        value = self.check_hex(value)
        try: dec = int(value, 16)
        except: return None
        return dec

    def get_os_from_build(self, build_number):
        # Returns the best-guess OS version for the build number
        alpha = "abcdefghijklmnopqrstuvwxyz"
        os_version = "Unknown"
        major = minor = ""
        try:
            # Formula looks like this:  AAB; AA - 4 = 10.## version
            # B index in "ABCDEFGHIJKLMNOPQRSTUVXYZ" = 10.##.## version
            split = re.findall(r"[^\W\d_]+|\d+", build_number)
            major = int(split[0])-4
            minor = alpha.index(split[1].lower())
            # Account for 11.0 at 10.16+
            osx   = 11 if major >= 16 else 10
            major = major-16 if major >= 16 else major
            os_version = "{}.{}.{}".format(osx, major, minor)
            # Python also has string comparisons so "10.15.0" > "10.14.0" would return True
        except:
            pass
        return os_version

    def discover_ports(self):
        # Iterates every 5 seconds showing any newly populated ports
        self.check_controllers()
        total_ports = OrderedDict()
        last_ports  = OrderedDict()
        last_list   = []
        pad = 11
        while True:
            extras = 0
            self.check_by_ioreg(force=True)
            self.u.head("Discover USB Ports")
            print("")
            check_ports = self.get_ports_and_devices()
            # Walk them and check for differences
            new_last_list = []
            for i,x in enumerate(check_ports):
                if len(check_ports[x]) > len(total_ports.get(x,[])): # Only append to keep track of all the items plugged in
                    total_ports[x] = [y for y in check_ports[x]]
                if last_ports and len(check_ports[x]) > len(last_ports.get(x,[])):
                    new_last_list.append((i+1,x))
            if new_last_list: last_list = [x for x in new_last_list] # Migrate the list over as needed
            # Snapshot the last seen ports to last_ports
            for x in check_ports:
                last_ports[x] = [y for y in check_ports[x]]
            # Enumerate the ports
            last_cont = None
            for index,port in enumerate(check_ports):
                n,p,a,c,r = port.split(" | ")
                if last_cont != c:
                    print("    ----- {}{} Controller{} -----".format(self.cs,r,self.ce))
                    last_cont = c
                    extras += 1
                print("{}{}. {}{}".format(
                    self.cs if any((port==x[1] for x in last_list)) else self.bs if len(total_ports.get(port,[])) else "",
                    index+1,
                    " | ".join(port.split(" | ")[:-2]),
                    self.ce if len(total_ports.get(port,[])) else ""
                ))
                # Initialize the last controller seen
                if last_cont == None: last_cont = c
                original = self.controllers[c]["ports"][p]
                merged_c = self.get_matching_controller(c,self.controllers,self.merged_list) # Try to get the merged version for comments, if possible
                merged_p = self.merged_list.get(merged_c,{}).get("ports",{}).get(p,{}) if merged_c else {}
                # Save the items if there were any
                if len(total_ports.get(port,[])):
                    new_items = original.get("items",[])
                    new_items.extend([x for x in total_ports[port] if not x in original.get("items",[])])
                    original["items"] = new_items
                    original["enabled"] = True
                if merged_p.get("comment",None):
                    extras += 1
                    print("    {}{}{}".format(self.nm, merged_p["comment"], self.ce))
                if len(check_ports[port]):
                    extras += len(check_ports[port])
                    print("\n".join(check_ports[port]))
            print("")
            # List the controllers and their port counts
            print("Populated:")
            pop_list = []
            for cont in self.controllers:
                count = self.get_populated_count_for_controller(cont)
                pop_list.append("{}{}: {:,}{}".format(
                    self.cs if 0 < count < 16 else self.rs,
                    cont.split("@")[0],
                    count,
                    self.ce
                ))
            print(", ".join(pop_list))
            temp_h = index+1+extras+pad+(1 if last_list else 0)
            h = temp_h if temp_h > 24 else 24
            self.u.resize(80, h)
            print("Press Q then [enter] to stop")
            if last_list:
                print("Press N then [enter] to nickname port{} {}".format(
                    "" if len(last_list)==1 else "s",
                    ", ".join([str(x[0]) for x in last_list])
                ))
            print("")
            out = self.u.grab("Waiting {:,} second{}:  ".format(self.discover_wait,"" if self.discover_wait == 1 else "s"), timeout=self.discover_wait)
            if not out or not len(out):
                continue
            if out.lower() == "q":
                break
            if out.lower() == "n" and last_list:
                # Let's set a nickname for this port
                self.get_name(last_list)
        self.merged_list = self.merge_controllers()
        self.save_plist()

    def get_name(self, port_list):
        # Helper method to add a custom name ("comment") to the passed ports
        # Gather the originals first
        originals = []
        name_list = []
        pad = 11
        # Fist ensure our merged_list is populated:
        self.merged_list = self.merge_controllers()
        # Iterate the ports
        for index,port in port_list:
            n,p,a,c,r = port.split(" | ")
            assert c in self.merged_list # Verify the controller is there
            assert p in self.merged_list[c]["ports"] # Verify the port is also there
            # Locate the original
            original = self.merged_list[c]["ports"][p]
            originals.append(original)
            nickname = original.get("comment",None)
            # Format and color the entry
            name_list.append("{}{}. {}{} = {}:\n{}".format(
                self.cs,
                index,
                n,
                self.ce,
                self.nm+nickname+self.ce if nickname else "None",
                "\n".join(original.get("items",[]))
            ))
        name_text = "\n".join(name_list)
        # Get the target window height
        temp_h = len(name_text.split("\n"))+pad
        h = temp_h if temp_h > 24 else 24
        self.u.resize(80, h)
        while True:
            # Display all the ports we intend to rename
            self.u.head("Port Nickname")
            print("")
            print("Current Port Numbers, Names, Nicknames and Devices:\n")
            print(name_text)
            print("")
            print("C. Clear Custom Names")
            print("Q. Return to Discovery")
            print("")
            menu = self.u.grab("Please type a nickname for port{} {}:  ".format(
                "" if len(port_list)==1 else "s",
                ", ".join([str(x[0]) for x in port_list])
            ))
            if not len(menu):
                continue
            if menu.lower() in ("c","none"):
                for original in originals:
                    original.pop("comment",None)
                return
            elif menu.lower() == "q":
                return
            for original in originals:
                original["comment"] = menu
            return

    def print_types(self):
        self.u.resize(80, 24)
        self.u.head("USB Types")
        print("")
        types = "\n".join([
            "0: Type A connector",
            "1: Mini-AB connector",
            "2: ExpressCard",
            "3: USB 3 Standard-A connector",
            "4: USB 3 Standard-B connector",
            "5: USB 3 Micro-B connector",
            "6: USB 3 Micro-AB connector",
            "7: USB 3 Power-B connector",
            "8: Type C connector - USB2-only",
            "9: Type C connector - USB2 and SS with Switch",
            "10: Type C connector - USB2 and SS without Switch",
            "11 - 254: Reserved",
            "255: Proprietary connector"
        ])
        print(types)
        print("")
        print("Per the ACPI 6.2 Spec.")
        print("")
        self.u.grab("Press [enter] to return to the menu...")
        return

    def edit_plist(self):
        os.chdir(os.path.dirname(os.path.realpath(__file__)))
        pad = 24
        while True:
            self.save_plist()
            ports = [] # An empty list for index purposees
            extras = 0
            self.u.head("Edit USB Ports")
            print("")
            if not self.merged_list:
                print("No ports have been discovered yet!".format(self.usb_list))
                print("Use the discovery mode from main menu first.")
                print("")
                return self.u.grab("Press [enter] to return to the menu...")
            index = 0
            counts = OrderedDict()
            for cont in self.merged_list:
                print("    ----- {}{} Controller{} -----".format(self.cs,self.merged_list[cont]["parent"],self.ce))
                extras += 1
                counts[cont] = 0
                for port_num in self.merged_list[cont]["ports"]:
                    index += 1
                    port = self.merged_list[cont]["ports"][port_num]
                    ports.append(port)
                    if port.get("enabled",False): counts[cont] += 1 # Increment the port counter for the selected controller
                    usb_connector = port.get("type_override", 3 if "XHCI" in self.merged_list[cont]["type"] else 0)
                    print("{}[{}] {}. {} | {} | Type {}{}".format(
                        self.bs if port.get("enabled",False) else "",
                        "#" if port.get("enabled",False) else " ",
                        index,
                        port["name"],
                        port["address"],
                        usb_connector,
                        self.ce if port.get("enabled",False) else ""
                    ))
                    if port.get("comment",None):
                        extras += 1
                        print("    {}{}{}".format(self.nm, port["comment"], self.ce))
                    if len(port.get("items",[])):
                        extras += len(port["items"])
                        print("\n".join(port["items"]))
            print("")
            # List the controllers and their port counts
            print("Populated:")
            pop_list = []
            for cont in counts:
                pop_list.append("{}{}: {:,}{}".format(
                    self.cs if 0 < counts[cont] < 16 else self.rs,
                    cont.split("@")[0],
                    counts[cont],
                    self.ce
                ))
            print(", ".join(pop_list))
            print("")
            print("K. Build USBMap.kext")
            print("A. Select All")
            print("N. Select None")
            print("P. Enable All Populated Ports")
            print("D. Disable All Empty Ports")
            print("T. Show Types")
            print("")
            print("M. Main Menu")
            print("Q. Quit")
            print("")
            print("- Select ports to toggle with comma-delimited lists (eg. 1,2,3,4,5)")
            print("- Change types using this formula T:1,2,3,4,5:t where t is the type")
            print("- Set custom names using this formula C:1:Name - Name = None to clear")
            print("- Enabled/Disable all controller ports with U:Cont:e where e is On/Off")
            print("    and Cont is the controller@address (eg U:XHC@14000000:On)")
            temp_h = index+1+extras+pad
            h = temp_h if temp_h > 24 else 24
            self.u.resize(80, h)
            menu = self.u.grab("Please make your selection:  ")
            if not len(menu):
                continue
            if menu.lower() == "q":
                self.u.resize(80, 24)
                self.u.custom_quit()
            elif menu.lower() == "m":
                return
            elif menu.lower() == "k":
                self.build_kext()
            elif menu.lower() in ("n","a"):
                # Iterate all ports and deselect them
                for port in ports:
                    port["enabled"] = True if menu.lower() == "a" else False
                continue
            elif menu.lower() == "p":
                # Select all populated ports
                for port in ports:
                    if port.get("items",[]): port["enabled"] = True
            elif menu.lower() == "d":
                # Deselect any empty ports
                for port in ports:
                    if not port.get("items",[]): port["enabled"] = False
            elif menu.lower() == "t":
                self.print_types()
                continue
            # Check if we need to toggle
            if menu[0].lower() == "t":
                # We should have a type
                try:
                    nums = [int(x) for x in menu.split(":")[1].replace(" ","").split(",")]
                    t = int(menu.split(":")[-1])
                    for x in nums:
                        x -= 1
                        if not 0 <= x < len(ports): continue # Out of bounds, skip
                        # Valid index
                        ports[x]["type_override"] = t
                except:
                    continue
            elif menu[0].lower() == "c":
                # We should have a new name
                try:
                    nums = [int(x) for x in menu.split(":")[1].replace(" ","").split(",")]
                    name = menu.split(":")[-1]
                    for x in nums:
                        x -= 1
                        if not 0 <= x < len(ports): continue # Out of bounds, skip
                        # Valid index
                        if name.lower() == "none": ports[x].pop("comment",None)
                        else: ports[x]["comment"] = name
                except:
                    continue
            elif menu[0].lower() == "u":
                # We should have a controller name, and on/off
                try:
                    cont = menu.split(":")[1]
                    toggle = menu.split(":")[-1].lower()
                    cont = next((x for x in self.merged_list if x.lower() == cont.lower())) # Normalize case
                    if not cont in self.merged_list or not toggle in ("on","off"): continue # Formatted wrong, ignore it
                    for port_num in self.merged_list[cont].get("ports",{}):
                        port = self.merged_list[cont]["ports"][port_num]
                        port["enabled"] = toggle == "on"
                except:
                    continue
            else:
                # Maybe a list of numbers?
                try:
                    nums = [int(x) for x in menu.replace(" ","").split(",")]
                    for x in nums:
                        x -= 1
                        if not 0 <= x < len(ports): continue # Out of bounds, skip
                        ports[x]["enabled"] = not ports[x].get("enabled",False)
                except:
                    continue

    def get_safe_acpi_path(self, path):
        return ".".join([x.split("@")[0] for x in path.split("/") if len(x) and not ":" in x])

    def get_numbered_name(self, base_name, number, use_hex=True):
        if use_hex: number = hex(number).replace("0x","").upper()
        else: number = str(number)
        return base_name[:-1*len(number)]+number

    def generate_renames(self, cont_list):
        used_names = [x for x in self.illegal_names]
        used_names.extend([self.connected_controllers[x]["parent_name"].upper() for x in self.connected_controllers if self.connected_controllers[x].get("parent_name",None)])
        self.u.head("Rename Devices")
        print("")
        ssdt = """//
// SSDT to rename PXSX, XHC1, EHC1, EHC2, and other conflicting device names
//
DefinitionBlock ("", "SSDT", 2, "CORP", "UsbReset", 0x00001000)
{
    /*
     * Start copying here if you're adding this info to an SSDT-USB-Reset!
     */

"""
        parents = []
        devices = []
        for cont in cont_list:
            con_type = "XHCI"
            print("Checking {}...".format(cont))
            c_type = self.connected_controllers[cont]["type"]
            acpi_path = self.get_safe_acpi_path(self.connected_controllers[cont]["acpi_path"])
            acpi_parent = ".".join(acpi_path.split(".")[:-1])
            acpi_addr = self.connected_controllers[cont]["acpi_address"]
            if "XHCI" in c_type:
                print(" - XHCI device")
            elif "EHCI" in c_type:
                print(" - EHCI device")
                con_type = "EH01"
            else: print(" - Unknown type - using XHCI")
            print(" - ACPI Path: {}".format(acpi_path))
            print(" --> ACPI Parent Path: {}".format(acpi_parent))
            print(" - ACPI _ADR: {}".format(acpi_addr))
            print(" - Gathering unique name...")
            # Now we have the base - let's increment!
            starting_number = 1 if con_type == "EH01" else 2
            while True:
                name = self.get_numbered_name(con_type,starting_number)
                if not name in used_names:
                    used_names.append(name)
                    break
                starting_number += 1
            # We should have a unique name here, add the info
            print(" --> Got {}".format(name))
            parents.append(acpi_parent)
            devices.append((acpi_path,name,acpi_addr,acpi_parent))
        print("Building SSDT-USB-Reset.dsl...")
        # Add the parents as needed
        for parent in sorted(list(set(parents))):
            ssdt += "    External ({}, DeviceObj)\n".format(parent)
        if len(parents): ssdt+="\n" # Add a newline after the parents for formatting
        for device in devices:
            # Get the info and build the SSDT
            acpi_path, name, acpi_addr, acpi_parent = device
            ssdt += "    External ({}, DeviceObj)\n".format(acpi_path)
            ssdt += """
    Scope([[device]])
    {
        Method (_STA, 0, NotSerialized)  // _STA: Status
        {
            If (_OSI ("Darwin"))
            {
                Return (Zero)
            }
            Else
            {
                Return (0x0F)
            }
        }
    }

    Scope([[parent]])
    {
        Device ([[new_device]])
        {
            Name (_ADR, [[address]])  // _ADR: Address
            Method (_STA, 0, NotSerialized)  // _STA: Status
            {
                If (_OSI ("Darwin"))
                {
                    Return (0x0F)
                }
                Else
                {
                    Return (Zero)
                }
            }
        }
    }

""".replace("[[device]]",acpi_path).replace("[[parent]]",acpi_parent).replace("[[new_device]]",name).replace("[[address]]",acpi_addr)
        # Add the footer
        ssdt += """    /*
     * End copying here if you're adding this info to an SSDT-USB-Reset!
     */
}"""
        print("Saving to SSDT-USB-Reset.dsl...")
        os.chdir(os.path.dirname(os.path.realpath(__file__)))
        if not os.path.exists(self.output): os.mkdir(self.output)
        with open(self.ssdt_path,"w") as f:
            f.write(ssdt)
        self.re.reveal(self.ssdt_path,True)
        print("")
        print("Done.")
        print("")
        self.u.grab("Press [enter] to return to the menu...")

    def reset_rhubs(self,rhub_paths):
        self.u.head("Reset RHUBs")
        print("")
        ssdt = """//
// SSDT to reset RHUB devices on XHCI controllers to force hardware querying of ports
//
// WARNING: May conflict with existing SSDT-USB-Reset!  Verify names and paths before
//          merging!
//
DefinitionBlock ("", "SSDT", 2, "CORP", "RHBReset", 0x00001000)
{
    /*
     * Start copying here if you're adding this info to an existing SSDT-USB-Reset!
     */

"""
        print("Building SSDT-RHUB-Reset.dsl...")
        for rhub in sorted(list(set(rhub_paths))):
            print("Resetting {}...".format(rhub))
            ssdt += "    External ({}, DeviceObj)\n".format(rhub)
            ssdt += """
    Scope([[device]])
    {
        Method (_STA, 0, NotSerialized)  // _STA: Status
        {
            If (_OSI ("Darwin"))
            {
                Return (Zero)
            }
            Else
            {
                Return (0x0F)
            }
        }
    }

""".replace("[[device]]",rhub)
        # Add the footer
        ssdt += """    /*
     * End copying here if you're adding this info to an SSDT-USB-Reset!
     */
}"""
        print("Saving to SSDT-RHUB-Reset.dsl...")
        os.chdir(os.path.dirname(os.path.realpath(__file__)))
        if not os.path.exists(self.output): os.mkdir(self.output)
        with open(self.rsdt_path,"w") as f:
            f.write(ssdt)
        self.re.reveal(self.rsdt_path,True)
        print("")
        print("Done.")
        print("")
        self.u.grab("Press [enter] to return to the menu...")

    def main(self):
        self.u.resize(80, 24)
        self.u.head("USBMap")
        print("")
        os.chdir(os.path.dirname(os.path.realpath(__file__)))
        print("Current Controllers:")
        print("")
        needs_rename = []
        rhub_paths   = []
        if not len(self.connected_controllers): print(" - {}None{}".format(self.rs,self.ce))
        else:
            # We have controllers - let's show them
            pad = max(len(self.connected_controllers[x]["parent"]) for x in self.connected_controllers)
            names = [self.connected_controllers[x]["parent_name"] for x in self.connected_controllers]
            for x in self.connected_controllers:
                if "locationid" in self.connected_controllers[x]: continue # don't show hubs in this list
                acpi = self.get_safe_acpi_path(self.connected_controllers[x].get("acpi_path","Unknown ACPI Path"))
                name = self.connected_controllers[x]["parent_name"]
                par  = self.connected_controllers[x]["parent"]
                if name in self.illegal_names or names.count(name) > 1:
                    needs_rename.append(x)
                    self.controllers.pop(x,None) # Remove it from the controllers to map
                    print(" - {}{}{} @ {} ({}{}{})".format(self.rs,par.rjust(pad),self.ce,acpi,self.rs,"Needs Rename" if name in self.illegal_names else "Not Unique",self.ce))
                else: print(" - {}{}{} @ {}".format(self.cs,par.rjust(pad),self.ce,acpi))
                if not "XHCI" in self.connected_controllers[x]["type"]: continue # Only check XHCI for RHUB paths
                # Get the RHUB name - mirrors the controller name if actually "RHUB"
                rhub_name = "RHUB" if x.split("@")[0].upper() == self.connected_controllers[x]["parent_name"] else x.split("@")[0].upper()
                rhub_path = ".".join([acpi,rhub_name])
                rhub_paths.append(rhub_path)
                print("  \\-> {}RHUB{} @ {}".format(self.bs,self.ce,rhub_path))
        print("")
        print("{}D. Discover Ports{}{}".format(
            self.rs if needs_rename else "",
            " (Will Ignore Invalid Controllers)" if needs_rename else "",
            self.ce
        ))
        print("{}P. Edit & Create USBMap.kext{}{}".format(
            "" if self.merged_list else self.rs,
            "" if self.merged_list else " (Must Discover Ports First)",
            self.ce
        ))
        print("R. Reset All Detected Ports")
        if needs_rename:
            print("A. Generate ACPI Renames For Conflicting Controllers")
        if rhub_paths:
            print("H. Generate ACPI To Reset RHUBs ({}May Conflict With Existing SSDT-USB-Reset.aml!{})".format(self.rs,self.ce))
        print("")
        print("Q.  Quit")
        print("")
        menu = self.u.grab("Please select an option:  ")
        if not len(menu):
            return
        if menu.lower() == "q":
            self.u.resize(80, 24)
            self.u.custom_quit()
        elif menu.lower() == "r":
            try:
                # Reset the merged_list and repopulate the controllers
                self.merged_list = OrderedDict()
                if os.path.exists(self.usb_list):
                    os.remove(self.usb_list)
            except Exception as e:
                print("Failed to remove USB.plist! {}".format(e))
            return
        elif menu.lower() == "d":
            self.discover_ports()
        elif menu.lower() == "p" and self.merged_list:
            self.edit_plist()
        elif menu.lower() == "a" and needs_rename:
            self.generate_renames(needs_rename)
        elif menu.lower() == "h" and rhub_paths:
            self.reset_rhubs(rhub_paths)

if __name__ == '__main__':
    u = USBMap()
    while True:
        u.main()
