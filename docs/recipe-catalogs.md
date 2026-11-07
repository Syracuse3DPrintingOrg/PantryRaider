# Your own recipe databases

Pantry Raider can read recipes straight from a recipe website you or your
family keep. Add the site once, and its recipes show up next to everything
else:

- **Recipe search**, with Other recipes switched on. Leave the search box empty
  to browse the whole database.
- **The Cook page**, where every recipe in the database is matched against
  what you have in stock, the same way web recipes are.
- **Preview, Cook and Save.** Open a recipe to read it, cook it without saving,
  or save a copy into your own library. A saved copy keeps a credit line and a
  link back to the original.

Your inventory never leaves your box. Pantry Raider downloads the database's
recipe list and does the matching locally.

## Adding a recipe database

1. Open **Settings**, then **Recipes**, and find **Your recipe databases**.
2. For a database Pantry Raider already knows, such as Deeserved, press its
   **Add** button.
3. For any other site, paste the address the site gives you (it usually ends in
   `/api/pantry-raider`), add an access key if the site gave you one, and press
   **Test** to check it answers, then **Add**.

Each database has a switch to hide its recipes for a while, and a remove
button. Removing a database leaves the recipes you already saved in your
library.

A Pi Remote shows the recipe databases its main server has, so add them on
the server.

### Deeserved

[Deeserved](https://deeserved.com) is a family recipe box of scanned and
transcribed recipe cards, and the first site to offer a recipe database. It
needs no access key: anyone can read its published recipes.

## For developers: serving a recipe database

Any website can become a recipe database by answering three read-only
requests. The protocol is called the **Pantry Raider Recipe Catalog API**,
version 1. Pantry Raider calls it from the server, never from a browser, so
CORS headers are not needed.

The user pastes your **base address**, for example
`https://example.com/api/pantry-raider`. Every path below hangs off it.

### Access keys

Keys are optional. A request without an `Authorization` header should get
your public recipes. If you hand out keys, Pantry Raider sends the key as
`Authorization: Bearer <key>` on every request, and a key you refuse should
get a 401 or 403 answer.

### GET {base}/catalog

Who you are. Pantry Raider calls this when the user presses Test or Add, and
refuses any address whose answer lacks `"api": "pantry-raider-catalog"`.

```json
{
  "api": "pantry-raider-catalog",
  "version": 1,
  "name": "Deeserved",
  "description": "Dee's recipe box",
  "url": "https://deeserved.com",
  "recipe_count": 58,
  "updated_at": "2026-11-07T14:02:11Z"
}
```

`name` becomes the database's name in Pantry Raider unless the user types
their own.

### GET {base}/recipes?q=&limit=&offset=

A page of recipe summaries. `limit` defaults to 50 and may be capped at 200,
`offset` defaults to 0, and an empty `q` lists everything. Sort however suits
you, but keep the order stable so pages do not overlap.

```json
{
  "total": 58,
  "offset": 0,
  "limit": 50,
  "recipes": [
    {
      "id": "87",
      "slug": "zuppa-toscana",
      "name": "Zuppa Toscana",
      "description": "Sausage, kale and sun-dried tomatoes in a creamy broth.",
      "image": "https://example.com/media/zuppa.webp",
      "url": "https://example.com/recipes/zuppa-toscana",
      "servings": "4 to 6",
      "total_time": "About 3 hours",
      "categories": ["Soups and salads"],
      "cuisine": "Italian",
      "author": "Liz",
      "updated_at": "2026-10-30T18:22:00Z",
      "ingredients": ["1 lb spicy sausage", "4 cups chopped kale"]
    }
  ]
}
```

Put the ingredient lines in the summary. Pantry Raider matches every recipe
against the user's stock from this listing alone, so it reads your whole
database in a few requests instead of one per recipe. It reads at most 2,000
recipes and keeps the listing for ten minutes.

### GET {base}/recipes/{id}

One full recipe: the summary fields plus the method.

```json
{
  "id": "87",
  "name": "Zuppa Toscana",
  "ingredients": ["1 lb spicy sausage", "4 cups chopped kale", "6 cups broth"],
  "ingredient_groups": [
    {"heading": null, "items": ["1 lb spicy sausage", "4 cups chopped kale"]},
    {"heading": "For the broth", "items": ["6 cups broth"]}
  ],
  "instructions": ["Brown the sausage.", "Combine everything in the slow cooker."],
  "instruction_groups": [
    {"heading": null, "items": ["Brown the sausage.", "Combine everything in the slow cooker."]}
  ],
  "notes": ["Works on the stove top in about 40 minutes."]
}
```

An unknown id should get a 404. Group headings are kept as section headings
when the user saves the recipe; without groups, the flat `ingredients` and
`instructions` lists are used.

### Field rules

| Field | Rule |
|---|---|
| `id` | Required, a string, stable for the life of the recipe |
| `name` | Required |
| `image`, `url` | Absolute `http(s)` addresses or null. Images must load without a key |
| `ingredients`, `instructions`, `notes` | Lists of plain-text lines, in order |
| `categories` | Display labels; they also feed the Cook page's cuisine filter |
| `total_time`, `servings` | Free text, shown as is |
| Everything else | Optional. Send null, an empty string or an empty list rather than leaving a field out |

Recipes without an `id` or a `name` are skipped. Pantry Raider never writes to
a recipe database.
