const fs = require("fs");
const vm = require("vm");

const html = fs.readFileSync("tools/personnel-history.html", "utf8");
const inlineScripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
  .map((match) => match[1])
  .filter((script) => script.trim());
for (const script of inlineScripts) new Function(script);

const ids = [...html.matchAll(/(?:\s|<)id="([^"]+)"/g)].map((match) => match[1]);
const duplicateIds = [...new Set(ids.filter((id, index) => ids.indexOf(id) !== index))];
if (duplicateIds.length) throw new Error(`Duplicate HTML ids: ${duplicateIds.join(", ")}`);

const context = { window: {} };
vm.createContext(context);
for (const file of ["tools/personnel-data.js", "tools/school-map-data.js"]) {
  vm.runInContext(fs.readFileSync(file, "utf8"), context, { filename: file });
}
const mapData = context.window.GN_SCHOOL_MAP_DATA;
if (!mapData?.schools?.length) throw new Error("School map data is empty");
const invalidCoordinates = mapData.schools.filter((school) => !Number.isFinite(school.lat) || !Number.isFinite(school.lon));
if (invalidCoordinates.length) throw new Error(`Invalid coordinates: ${invalidCoordinates.length}`);
const districts = new Set(mapData.schools.map((school) => school.district));
if (districts.size !== 18) throw new Error(`Expected 18 districts, found ${districts.size}`);

console.log(JSON.stringify({ inlineScripts: inlineScripts.length, htmlIds: ids.length, districts: districts.size, ...mapData.stats }));
