// System.Diagnostics.CodeAnalysis supplies [NotNullWhen]/[MaybeNullWhen], which
// is how a TryParse/TryGet pattern stays honest under <Nullable>enable: the out
// parameter is declared nullable and the attribute tells callers it is non-null
// whenever the method returns true.
global using System.Diagnostics.CodeAnalysis;
global using System.Globalization;
global using System.Text;
global using System.Text.Json;
