import { createContext, useContext } from 'react';

const SearchContext = createContext({ query: '', setQuery: () => {} });

export default SearchContext;

export function useSearch() {
  return useContext(SearchContext);
}
