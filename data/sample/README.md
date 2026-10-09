# Demo dataset

`messy_customers.csv` is a controlled synthetic example, not a public dataset. It deliberately includes inconsistent surrounding whitespace, an identifier that resembles a number, mixed date formats (including ambiguous month/day values), a missing-value sentinel (`unknown`), an exact duplicate record, a category capitalization variant, and currency-formatted numeric text. The intended safe behavior is to profile all issues, offer low-risk whitespace trimming, preserve identifiers and ambiguous values, and ask before row removal or category mapping.
