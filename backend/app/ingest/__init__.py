"""Statement ingestion package.

Each parser turns a raw source file (Apple Card CSV, Wells Fargo PDF) into a
list of CanonicalRecord. The pipeline reconciles, dedupes, and writes them as
Transaction rows. All money is signed integer cents end to end: there is never
a float in the path from a printed total to a stored amount.
"""
