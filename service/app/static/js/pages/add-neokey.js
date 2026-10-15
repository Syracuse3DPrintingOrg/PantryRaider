// This device has a NeoKey pad beside the screen (FoodAssistant-3alc): mark
// the page so the kiosk layout can stand the mode tiles up as a column on
// the right edge. Loaded right where the tiles start so there is no flash
// of the wide grid.

document.documentElement.classList.add('pr-neokey');
