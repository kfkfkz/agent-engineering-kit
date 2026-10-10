# External-key compatibility decision

The upstream integration treats external keys as opaque, case-sensitive strings. Leading zeros,
letter case, punctuation and whitespace are part of identity. Return the supplied string unchanged;
do not trim, lowercase, parse as a number, or share display-name normalization with the key path.

This is a fixed synthetic decision supplied identically to both experimental variants.
